"""Local typed formal-freeze admission for Luanti #14757.

This is a pre-annotation freeze receipt, not authorization to call a paid
provider or to run formal trials.  It validates the current Gate 1--7,
server Gate 9/10, and HUDfix-v4 Gate 11/12/13 heads plus the normal v2
curated admission.  No process is started and no historical root is reused.
"""

from __future__ import annotations

import stat
from pathlib import Path

from .curated import (
    _load_catalog_task,
    _load_v2_admission,
    _load_v2_transition_receipt,
)
from .models import CandidateKey
from .population import load_v2_candidate_eligibility, load_v2_enumeration_audit
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


CANDIDATE = CandidateKey("luanti", 14757)
TASK_ID = "lz-luanti-14757-math-round"
_SEMANTIC_VERSION = "2.0"
_ARTIFACT = ArtifactReference(
    "candidate_formal_freeze_admission/luanti/14757/current/v2",
    "candidate_formal_freeze_admission",
    _SEMANTIC_VERSION,
)
_AGGREGATE = ArtifactReference(
    "lua_regression_aggregate/luanti/14757/fixed-buggy/v3",
    "lua_regression_aggregate",
    "3.0",
)
_TRANSITION = ArtifactReference(
    "luanti_lua_regression_transition/luanti/14757/test-repetitions/v2",
    "luanti_lua_regression_transition",
    "2.0",
)
_GATE9 = ArtifactReference(
    "candidate_gate9_server_side_replay/luanti/14757/repackage-v3/v1",
    "candidate_gate9_server_side_replay",
    "1.0",
)
_GATE10_OBSERVATION = ArtifactReference(
    "candidate_gate10_server_side_observation/luanti/14757/repackage-v3/v1",
    "candidate_gate10_server_side_observation",
    "1.0",
)
_GATE10_ORACLE = ArtifactReference(
    "candidate_gate10_server_side_oracle/luanti/14757/repackage-v3/v1",
    "candidate_gate10_server_side_oracle",
    "1.0",
)
_GATE11 = ArtifactReference(
    "candidate_gate11_f12_hudfix_observation/luanti/14757/repackage-v3/v1",
    "candidate_gate11_f12_hudfix_observation",
    "1.0",
)
_GATE12 = ArtifactReference(
    "candidate_gate12_f12_hudfix_matrix/luanti/14757/repackage-v3/v1",
    "candidate_gate12_f12_hudfix_matrix",
    "1.0",
)
_GATE13 = ArtifactReference(
    "candidate_gate13_f12_hudfix_construction/luanti/14757/repackage-v3/v1",
    "candidate_gate13_f12_hudfix_construction",
    "1.0",
)
_GATE13_RECEIPT = ArtifactReference(
    "candidate_gate13_f12_hudfix_receipt/luanti/14757/repackage-v3/v1",
    "candidate_gate13_f12_hudfix_receipt",
    "1.0",
)
_CURATED_LINEAGE = ArtifactReference(
    "candidate_curated_lineage_evidence/luanti/14757/current/v2",
    "candidate_curated_lineage_evidence",
    _SEMANTIC_VERSION,
)
_CURATED_ADMISSION = ArtifactReference(
    "curated_task_admission/luanti/14757/current/v2",
    "curated_task_admission",
    _SEMANTIC_VERSION,
)


def write_luanti_14757_formal_freeze_admission(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    catalog_path: str | Path = "benchmarks/p5/lazarus/curated.yaml",
    workspace_root: str | Path = ".",
) -> StableArtifact:
    """Validate current evidence and persist one local freeze-admission head."""

    root = Path(artifact_root).absolute().resolve(strict=True)
    workspace = Path(workspace_root).absolute().resolve(strict=True)
    artifacts = _load_required_heads(root)
    task = _load_catalog_task(TASK_ID, catalog_path, workspace)
    if CandidateKey(task.repository_key, task.change_number) != CANDIDATE:
        raise RuntimeError("#14757 formal freeze catalog candidate mismatch")
    eligibility = load_v2_candidate_eligibility(root, CANDIDATE)
    audit = load_v2_enumeration_audit(root, CANDIDATE)
    transition = _load_v2_transition_receipt(root, task, eligibility, audit)
    admission = _load_v2_admission(root, task, eligibility, audit, transition)
    _validate_gate_fields(root, artifacts)

    payload: dict[str, object] = {
        "candidate": CANDIDATE.to_value(),
        "task_id": TASK_ID,
        "issue_number": task.issue_number,
        "defect_locus": task.defect_locus,
        "freeze_state": "admitted",
        "formal_freeze_eligible": True,
        "formal_trials_authorized": False,
        "paid_actions_authorized": False,
        "annotation_authorized": False,
        "source_patch_test_gate": "qualified_3x_f2p_p2p",
        "gate9_status": "passed_ordered_replay",
        "gate10_status": "passed_hidden_oracle_and_semantic_observations",
        "gate11_status": "passed_real_engine_frames",
        "gate12_status": "passed_before_equivalence_and_after_2x2",
        "gate13_status": "passed_same_lineage_construction",
        "before_visual_equivalence": True,
        "after_2x2_cross_matrix": True,
        "same_lineage": True,
        "no_timeout_or_unknown": True,
        "current_v2_checkpoint": eligibility.preannotation_checkpoint.reference.artifact_id,
        "curated_admission": admission.reference.artifact_id,
        "catalog_receipt": {
            "catalog_path": str(Path(catalog_path)),
            "selection_mode": "curated_v1",
            "task_id": TASK_ID,
            "candidate": CANDIDATE.to_value(),
            "defect_locus": task.defect_locus,
            "issue_number": task.issue_number,
        },
        "complete": True,
    }
    inputs = (
        _AGGREGATE,
        _TRANSITION,
        _GATE9,
        _GATE10_OBSERVATION,
        _GATE10_ORACLE,
        _GATE11,
        _GATE12,
        _GATE13,
        _GATE13_RECEIPT,
        _CURATED_LINEAGE,
        eligibility.preannotation_checkpoint.reference,
        transition.reference,
        admission.reference,
    )
    _write_once(root, inputs, payload, authorization)
    return load_stable_artifact(
        root,
        artifact_id=_ARTIFACT.artifact_id,
        artifact_kind=_ARTIFACT.artifact_kind,
        semantic_version=_ARTIFACT.semantic_version,
    )


def _load_required_heads(root: Path) -> dict[str, StableArtifact]:
    refs = {
        "aggregate": _AGGREGATE,
        "transition": _TRANSITION,
        "gate9": _GATE9,
        "gate10_observation": _GATE10_OBSERVATION,
        "gate10_oracle": _GATE10_ORACLE,
        "gate11": _GATE11,
        "gate12": _GATE12,
        "gate13": _GATE13,
        "gate13_receipt": _GATE13_RECEIPT,
        "curated_lineage": _CURATED_LINEAGE,
    }
    return {name: _load(root, ref) for name, ref in refs.items()}


def _validate_gate_fields(root: Path, artifacts: dict[str, StableArtifact]) -> None:
    aggregate = artifacts["aggregate"].payload
    transition = artifacts["transition"].payload
    if (
        aggregate.get("candidate") != CANDIDATE.to_value()
        or aggregate.get("qualified") is not True
        or aggregate.get("all_expected") is not True
        or aggregate.get("stable_repetitions") is not True
        or aggregate.get("repetitions_per_cell") != 3
        or aggregate.get("attempt_count") != 12
        or transition.get("candidate") != CANDIDATE.to_value()
        or transition.get("qualified") is not True
        or transition.get("complete") is not True
        or transition.get("stable_repetitions") is not True
        or transition.get("attempt_count") != 12
        or transition.get("truth_table")
        != {
            "fixed": {"f2p": True, "p2p": True},
            "buggy": {"f2p": False, "p2p": True},
        }
    ):
        raise RuntimeError("#14757 Gate 1-7 current transition is not qualified")

    gate9 = artifacts["gate9"].payload
    if (
        gate9.get("state") != "passed"
        or gate9.get("gate9_status") != "complete"
        or gate9.get("ordered_replay")
        != [
            {"type": "restore_registered_start"},
            {
                "command": "/c1_lazarus_round",
                "type": "luanti_worldmod_math_round_boundary",
            },
        ]
        or gate9.get("raw_start_observations") is not True
        or gate9.get("candidate_execution_performed") is not True
        or gate9.get("old_attempts_loaded") is not False
        or gate9.get("old_construction_loaded") is not False
    ):
        raise RuntimeError("#14757 Gate 9 current replay is incomplete")

    observation = artifacts["gate10_observation"].payload
    oracle = artifacts["gate10_oracle"].payload
    if (
        observation.get("state") != "passed"
        or observation.get("hidden_oracle") is not True
        or observation.get("real_semantic_observations") is not True
        or observation.get("invariants", {}).get("world_responsive") is not True
        or observation.get("invariants", {}).get("marker_and_result_same_replay")
        is not True
        or oracle.get("state") != "passed"
        or oracle.get("gate10_status") != "complete"
        or oracle.get("hidden_oracle") is not True
        or oracle.get("real_semantic_observations") is not True
    ):
        raise RuntimeError("#14757 Gate 10 current oracle is incomplete")

    gate11 = artifacts["gate11"].payload
    gate12 = artifacts["gate12"].payload
    gate13 = artifacts["gate13"].payload
    receipt = artifacts["gate13_receipt"].payload
    if (
        gate11.get("state") != "passed"
        or gate11.get("gate11_status") != "semantic_frames_captured"
        or gate11.get("synthetic_frames") is not False
        or gate11.get("old_attempts_loaded") is not False
        or gate11.get("old_construction_loaded") is not False
        or gate12.get("state") != "passed"
        or gate12.get("gate12_status") != "passed_before_and_after_2x2"
        or gate12.get("matrix", {}).get("complete") is not True
        or gate12.get("matrix", {}).get("before_equivalent") is not True
        or gate12.get("matrix", {}).get("after_fixed_buggy_different") is not True
        or gate12.get("matrix", {}).get("synthetic_frames") is not False
        or gate13.get("state") != "qualified"
        or gate13.get("same_lineage_source_pair") is not True
        or gate13.get("production_test_bytes_changed") is not False
        or gate13.get("old_attempts_loaded") is not False
        or gate13.get("old_construction_loaded") is not False
        or receipt.get("state") != "passed"
        or receipt.get("gate11_status") != "passed_real_engine_frames"
        or receipt.get("gate12_status") != "passed_before_and_after_2x2"
        or receipt.get("matrix", {}).get("before_equivalent") is not True
        or receipt.get("matrix", {}).get("after_fixed_buggy_different") is not True
        or receipt.get("matrix", {}).get("synthetic_frames") is not False
        or receipt.get("production_test_bytes_changed") is not False
        or receipt.get("old_attempts_loaded") is not False
        or receipt.get("old_construction_loaded") is not False
    ):
        raise RuntimeError(
            "#14757 HUDfix-v4 current visual/construction fields are incomplete"
        )

    for name, reference in (
        (
            "gate9_fixed",
            "candidate_gate9_server_side_attempt/luanti/14757/repackage-v3/fixed/1/v1",
        ),
        (
            "gate9_buggy",
            "candidate_gate9_server_side_attempt/luanti/14757/repackage-v3/buggy/1/v1",
        ),
        (
            "gate11_fixed",
            "candidate_gate11_f12_hudfix_attempt/luanti/14757/repackage-v3/fixed/1/v1",
        ),
        (
            "gate11_buggy",
            "candidate_gate11_f12_hudfix_attempt/luanti/14757/repackage-v3/buggy/1/v1",
        ),
    ):
        kind = (
            "candidate_gate9_server_side_attempt"
            if name.startswith("gate9")
            else "candidate_gate11_f12_hudfix_attempt"
        )
        attempt = _load(
            root,
            ArtifactReference(reference, kind, "1.0"),
        )
        if (
            attempt.payload.get("process_outcome") != "completed"
            or attempt.payload.get("returncode") != 0
            or attempt.payload.get("terminal") is not False
            or attempt.payload.get("old_attempts_loaded") is not False
            or attempt.payload.get("old_construction_loaded") is not False
        ):
            raise RuntimeError(f"#14757 {name} has timeout/unknown execution state")

    for key in ("fixed_before", "fixed_after", "buggy_before", "buggy_after"):
        relative = gate11.get(key)
        if type(relative) is not str:
            raise RuntimeError(f"#14757 {key} frame path is missing")
        raw_path = root / relative
        if raw_path.is_symlink():
            raise RuntimeError(f"#14757 {key} frame path is unsafe")
        path = raw_path.resolve(strict=False)
        if not path.is_relative_to(root):
            raise RuntimeError(f"#14757 {key} frame path is unsafe")
        try:
            mode = path.stat().st_mode
        except OSError as error:
            raise RuntimeError(f"#14757 {key} frame is missing") from error
        if not stat.S_ISREG(mode):
            raise RuntimeError(f"#14757 {key} frame is not a regular file")


def _write_once(
    root: Path,
    inputs: tuple[ArtifactReference, ...],
    payload: dict[str, object],
    authorization: AuthorizationRecord,
) -> None:
    existing: StableArtifact | None
    try:
        existing = _load(root, _ARTIFACT)
    except RuntimeError as error:
        if str(error) in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            existing = None
        else:
            raise
    if existing is not None:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError("#14757 formal freeze admission current head conflicts")
        return
    from datetime import UTC, datetime

    write_stable_artifact(
        root,
        artifact_id=_ARTIFACT.artifact_id,
        artifact_kind=_ARTIFACT.artifact_kind,
        semantic_version=_ARTIFACT.semantic_version,
        created_at=datetime.now(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z"),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


__all__ = [
    "CANDIDATE",
    "TASK_ID",
    "write_luanti_14757_formal_freeze_admission",
]
