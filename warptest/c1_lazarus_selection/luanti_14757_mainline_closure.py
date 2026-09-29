"""Integrate the already typed Luanti #14757 HUDfix-v4 evidence into v2.

This adapter is deliberately non-executing.  It only loads the existing
source/patch chain and the already persisted repackage/server/F12 receipts,
then advances the normal v2 checkpoint with candidate-specific evidence
bridges.  The bridges do not reproduce observations or claim a new run.

The historical HUDfix receipts use the downstream v1 schema while the normal
pre-annotation DAG requires v2 evidence references.  The bridge artifacts are
therefore a narrow, explicit v1-read/v2-write boundary.  Curated admission is
attempted only when the catalog and its legacy construction lineage contain
this candidate; otherwise a nonterminal typed blocker is written and the
checkpoint stops at ``annotation_eligibility``.
"""

from __future__ import annotations

import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Mapping

from .curated import (
    load_curated_catalog,
    write_curated_construction_transition_receipt_v2,
    write_curated_task_admission_v2,
)
from .models import CandidateKey
from .pipeline import (
    PREANNOTATION_STAGES,
    StageDecision,
    run_preannotation_pipeline,
)
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


CANDIDATE = CandidateKey("luanti", 14757)
_SEMANTIC_VERSION = "2.0"
_MAPPING = ArtifactReference(
    "lazarus_task_mapping/luanti/14757/lz-luanti-14757-math-round/v4",
    "lazarus_task_mapping",
    "4.0",
)
_SOURCE_CAPSULE = ArtifactReference(
    "fixed_source_capsule/luanti/14757/fixed/v2", "fixed_source_capsule", "2.0"
)
_REPACKAGE = ArtifactReference(
    "candidate_downstream_repackage_receipt/luanti/14757/repackage-v3/v1",
    "candidate_downstream_repackage_receipt",
    "1.0",
)
_SERVER_REPLAY = ArtifactReference(
    "candidate_gate9_server_side_replay/luanti/14757/repackage-v3/v1",
    "candidate_gate9_server_side_replay",
    "1.0",
)
_SERVER_OBSERVATION = ArtifactReference(
    "candidate_gate10_server_side_observation/luanti/14757/repackage-v3/v1",
    "candidate_gate10_server_side_observation",
    "1.0",
)
_SERVER_ORACLE = ArtifactReference(
    "candidate_gate10_server_side_oracle/luanti/14757/repackage-v3/v1",
    "candidate_gate10_server_side_oracle",
    "1.0",
)
_HUDFIX_CONSTRUCTION = ArtifactReference(
    "candidate_gate13_f12_hudfix_construction/luanti/14757/repackage-v3/v1",
    "candidate_gate13_f12_hudfix_construction",
    "1.0",
)
_HUDFIX_OBSERVATION = ArtifactReference(
    "candidate_gate11_f12_hudfix_observation/luanti/14757/repackage-v3/v1",
    "candidate_gate11_f12_hudfix_observation",
    "1.0",
)
_HUDFIX_MATRIX = ArtifactReference(
    "candidate_gate12_f12_hudfix_matrix/luanti/14757/repackage-v3/v1",
    "candidate_gate12_f12_hudfix_matrix",
    "1.0",
)
_HUDFIX_RECEIPT = ArtifactReference(
    "candidate_gate13_f12_hudfix_receipt/luanti/14757/repackage-v3/v1",
    "candidate_gate13_f12_hudfix_receipt",
    "1.0",
)
_BRIDGE_PREFIX = "candidate_mainline_evidence/luanti/14757"
_BLOCKER = ArtifactReference(
    "candidate_mainline_admission_blocker/luanti/14757/current/v2",
    "candidate_mainline_admission_blocker",
    _SEMANTIC_VERSION,
)


@dataclass(frozen=True)
class MainlineClosureResult:
    """The v2 stages and the optional curated admission boundary."""

    stages: tuple[StableArtifact, ...]
    checkpoint: StableArtifact
    blocker: StableArtifact
    transition: StableArtifact | None
    admission: StableArtifact | None


def integrate_luanti_14757_mainline(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    catalog_path: str | Path = "benchmarks/p5/lazarus/curated.yaml",
    workspace_root: str | Path = ".",
) -> MainlineClosureResult:
    """Advance #14757's v2 DAG without starting a candidate process.

    Existing Gate 1--7, server Gate 9/10, and HUDfix F12 Gate 11/12/13
    receipts are loaded first.  Only if all their typed predicates pass are
    v2 evidence bridges and post-admission stages written.  A missing catalog
    row/legacy construction lineage is a schema boundary, not a reason to
    invent a curated admission; in that case the final stage is a typed
    nonterminal infrastructure blocker.
    """

    root = Path(artifact_root).absolute().resolve(strict=True)
    workspace = Path(workspace_root).absolute().resolve(strict=True)
    _load_and_validate_inputs(root)
    bridges = _write_or_load_bridges(root, authorization=authorization)
    blocker = _write_or_load_admission_blocker(
        root,
        authorization=authorization,
        mapping=_load(root, _MAPPING),
        catalog_path=Path(catalog_path),
        workspace_root=workspace,
    )

    def _operation(stage: str) -> Callable[[CandidateKey], StageDecision]:
        if stage == "version_materialization":
            return lambda candidate: _decision_for(candidate, bridges[stage])
        if stage == "frozen_replay":
            return lambda candidate: _decision_for(candidate, bridges[stage])
        if stage == "hidden_oracle":
            return lambda candidate: _decision_for(candidate, bridges[stage])
        if stage == "construction_gate":
            return lambda candidate: _decision_for(candidate, bridges[stage])
        if stage == "annotation_eligibility":
            return lambda candidate: _blocked_decision(candidate, blocker)
        return lambda candidate: _unexpected_existing_stage(candidate, stage)

    operations = {stage: _operation(stage) for stage in PREANNOTATION_STAGES}
    run_preannotation_pipeline(
        root,
        candidates=(CANDIDATE,),
        operations=operations,
        authorization=authorization,
        selection_epoch=_selection_epoch_v2(),
        test_workers=1,
        build_workers=1,
        replay_workers=1,
        persistence_workers=1,
        reload_workers=1,
        queue_limit=1,
    )
    stages = tuple(
        _load(
            root,
            ArtifactReference(
                f"candidate_stage/luanti/14757/{stage}/v2",
                "candidate_stage",
                _SEMANTIC_VERSION,
            ),
        )
        for stage in PREANNOTATION_STAGES
        if _artifact_present(
            root,
            ArtifactReference(
                f"candidate_stage/luanti/14757/{stage}/v2",
                "candidate_stage",
                _SEMANTIC_VERSION,
            ),
        )
    )
    checkpoint = _load(
        root,
        ArtifactReference(
            "preannotation_checkpoint/luanti/14757/current/v2",
            "preannotation_checkpoint",
            _SEMANTIC_VERSION,
        ),
    )

    transition: StableArtifact | None = None
    admission: StableArtifact | None = None
    task_id = _mapping_task_id(_load(root, _MAPPING))
    if _catalog_contains(
        catalog_path=Path(catalog_path), workspace_root=workspace, task_id=task_id
    ):
        # The catalog branch remains fail-closed: admission is written only
        # after the exact task row and all three typed repetitions reload.
        transition = write_curated_construction_transition_receipt_v2(
            root,
            task_id=task_id,
            catalog_path=Path(catalog_path),
            workspace_root=workspace,
            original_selection_artifact_id="curated_selection/all/current/v1",
            original_revalidation_artifact_id=(
                "curated_task_revalidation/luanti/14757/construction"
            ),
            authorization=authorization,
        )
        admission = write_curated_task_admission_v2(
            root,
            task_id=task_id,
            catalog_path=Path(catalog_path),
            workspace_root=workspace,
            transition_artifact_id=transition.reference.artifact_id,
            authorization=authorization,
        )
    return MainlineClosureResult(
        stages=stages,
        checkpoint=checkpoint,
        blocker=blocker,
        transition=transition,
        admission=admission,
    )


def _selection_epoch_v2():
    # Local import avoids importing the epoch module while collecting simple
    # schema/loader tests.
    from .epoch import SELECTION_EPOCH_V2

    return SELECTION_EPOCH_V2


def _decision_for(candidate: CandidateKey, bridge: StableArtifact) -> StageDecision:
    if candidate != CANDIDATE:
        raise RuntimeError("#14757 mainline bridge received the wrong candidate")
    if (
        bridge.payload.get("state") != "passed"
        or bridge.payload.get("complete") is not True
    ):
        raise RuntimeError("#14757 mainline evidence bridge is not passed")
    return StageDecision.passed(
        {
            "evidence_artifacts": [
                {
                    "artifact_id": bridge.reference.artifact_id,
                    "artifact_kind": bridge.reference.artifact_kind,
                    "semantic_version": bridge.reference.semantic_version,
                }
            ],
            "evidence_status": bridge.payload.get("evidence_status"),
            "candidate_execution_performed": bridge.payload.get(
                "candidate_execution_performed"
            ),
        }
    )


def _blocked_decision(
    candidate: CandidateKey, blocker: StableArtifact
) -> StageDecision:
    if candidate != CANDIDATE:
        raise RuntimeError("#14757 mainline blocker received the wrong candidate")
    return StageDecision.terminal(
        "infrastructure_blocked",
        {
            "evidence_artifacts": [
                {
                    "artifact_id": blocker.reference.artifact_id,
                    "artifact_kind": blocker.reference.artifact_kind,
                    "semantic_version": blocker.reference.semantic_version,
                }
            ],
            "evidence_status": "curated_admission_schema_blocked",
        },
    )


def _unexpected_existing_stage(candidate: CandidateKey, stage: str) -> StageDecision:
    raise RuntimeError(
        f"#14757 refuses to recreate already completed v2 stage {stage!r} for {candidate}"
    )


def _load_and_validate_inputs(root: Path) -> None:
    """Load the exact current heads and verify their semantic predicates."""

    # The v2 source/patch/atomicity/test stages are validated by the normal
    # pipeline checkpoint loader.  Loading them here gives this adapter a
    # fail-closed boundary before any bridge is written.
    for reference in (
        ArtifactReference(
            "candidate_source_packet/luanti/14757/source/v2",
            "candidate_source_packet",
            _SEMANTIC_VERSION,
        ),
        ArtifactReference(
            "candidate_test_patch/luanti/14757/screening/v2",
            "candidate_test_patch",
            _SEMANTIC_VERSION,
        ),
        ArtifactReference(
            "source_atomicity/luanti/14757/preannotation/v2",
            "source_atomicity",
            _SEMANTIC_VERSION,
        ),
        ArtifactReference(
            "candidate_stage/luanti/14757/test_manifest/v2",
            "candidate_stage",
            _SEMANTIC_VERSION,
        ),
        ArtifactReference(
            "candidate_stage/luanti/14757/test_repetitions/v2",
            "candidate_stage",
            _SEMANTIC_VERSION,
        ),
    ):
        _load(root, reference)

    mapping = _load(root, _MAPPING)
    if (
        mapping.payload.get("candidate") != CANDIDATE.to_value()
        or mapping.payload.get("task_id") != "lz-luanti-14757-math-round"
        or mapping.payload.get("defect_locus") != "at_goal"
        or mapping.payload.get("complete") is not True
        or mapping.payload.get("candidate_execution_performed") is not False
    ):
        raise RuntimeError("#14757 task mapping is not the registered v4 mapping")

    repackage = _load(root, _REPACKAGE)
    rp = repackage.payload
    if (
        rp.get("candidate") != CANDIDATE.to_value()
        or rp.get("complete") is not True
        or rp.get("state") != "downstream_pending"
        or rp.get("outcome") != "static_packaging_qualified"
        or rp.get("terminal") is not False
        or rp.get("same_lineage_construction_receipt_complete") is not True
        or rp.get("goldens_complete") is not False
        or rp.get("visual_cross_matrix_complete") is not False
    ):
        raise RuntimeError("#14757 static repackage receipt is not current")
    for key in ("binding_application_fixed_path", "binding_application_buggy_path"):
        _require_application_bundle(root, rp[key])

    for reference, state_key in (
        (_SERVER_REPLAY, "server_replay"),
        (_SERVER_OBSERVATION, "server_observation"),
        (_SERVER_ORACLE, "server_oracle"),
    ):
        artifact = _load(root, reference)
        if (
            artifact.payload.get("state") != "passed"
            or artifact.payload.get("complete") is not True
            or artifact.payload.get("candidate_execution_performed") is not True
        ):
            raise RuntimeError(f"#14757 {state_key} receipt is not passed")

    construction = _load(root, _HUDFIX_CONSTRUCTION)
    if (
        construction.payload.get("state") != "qualified"
        or construction.payload.get("complete") is not True
        or construction.payload.get("candidate_execution_performed") is not True
    ):
        raise RuntimeError("#14757 HUDfix construction receipt is not qualified")
    observation = _load(root, _HUDFIX_OBSERVATION)
    matrix = _load(root, _HUDFIX_MATRIX)
    final = _load(root, _HUDFIX_RECEIPT)
    fp = final.payload
    if (
        observation.payload.get("state") != "passed"
        or observation.payload.get("gate11_status") != "semantic_frames_captured"
        or matrix.payload.get("state") != "passed"
        or matrix.payload.get("gate12_status") != "passed_before_and_after_2x2"
        or fp.get("candidate") != CANDIDATE.to_value()
        or fp.get("gate11_status") != "passed_real_engine_frames"
        or fp.get("gate12_status") != "passed_before_and_after_2x2"
        or fp.get("gate13_status") != "pending_mainline_final_checkpoint"
        or fp.get("formal_freeze_eligible") is not False
        or fp.get("candidate_execution_performed") is not True
        or fp.get("synthetic_frames") is not False
        or fp.get("matrix", {}).get("complete") is not True
        or fp.get("matrix", {}).get("before_equivalent") is not True
        or fp.get("matrix", {}).get("after_fixed_buggy_different") is not True
        or fp.get("production_test_bytes_changed") is not False
        or fp.get("old_attempts_loaded") is not False
        or fp.get("old_construction_loaded") is not False
    ):
        raise RuntimeError("#14757 HUDfix final receipt is not the passed v4 lineage")


def _write_or_load_bridges(
    root: Path, *, authorization: AuthorizationRecord
) -> Mapping[str, StableArtifact]:
    repackage = _load(root, _REPACKAGE)
    refs = {
        "version_materialization": ArtifactReference(
            f"{_BRIDGE_PREFIX}/version_materialization/v2",
            "candidate_mainline_evidence",
            _SEMANTIC_VERSION,
        ),
        "frozen_replay": ArtifactReference(
            f"{_BRIDGE_PREFIX}/frozen_replay/v2",
            "candidate_mainline_evidence",
            _SEMANTIC_VERSION,
        ),
        "hidden_oracle": ArtifactReference(
            f"{_BRIDGE_PREFIX}/hidden_oracle/v2",
            "candidate_mainline_evidence",
            _SEMANTIC_VERSION,
        ),
        "construction_gate": ArtifactReference(
            f"{_BRIDGE_PREFIX}/construction_gate/v2",
            "candidate_mainline_evidence",
            _SEMANTIC_VERSION,
        ),
    }
    payloads = {
        "version_materialization": {
            "candidate": CANDIDATE.to_value(),
            "stage": "version_materialization",
            "state": "passed",
            "evidence_status": "persistent_fixed_buggy_application_pair",
            "source_capsule_artifact_id": _SOURCE_CAPSULE.artifact_id,
            "fixed_application_path": repackage.payload[
                "binding_application_fixed_path"
            ],
            "buggy_application_path": repackage.payload[
                "binding_application_buggy_path"
            ],
            "candidate_execution_performed": False,
            "complete": True,
        },
        "frozen_replay": {
            "candidate": CANDIDATE.to_value(),
            "stage": "frozen_replay",
            "state": "passed",
            "evidence_status": "registered_start_ordered_replay_reloaded",
            "registered_start": True,
            "ordered_replay": True,
            "server_semantic_replay_reloaded": True,
            "hudfix_recovery_pair_reloaded": True,
            "candidate_execution_performed": True,
            "complete": True,
        },
        "hidden_oracle": {
            "candidate": CANDIDATE.to_value(),
            "stage": "hidden_oracle",
            "state": "passed",
            "evidence_status": "semantic_oracle_and_real_observations_reloaded",
            "hidden_oracle": True,
            "semantic_observations": True,
            "visual_cross_matrix_reloaded": True,
            "candidate_execution_performed": True,
            "complete": True,
        },
        "construction_gate": {
            "candidate": CANDIDATE.to_value(),
            "stage": "construction_gate",
            "state": "passed",
            "evidence_status": "hudfix_same_lineage_construction_reloaded",
            "construction_receipt_qualified": True,
            "same_lineage": True,
            "gate13_mainline_integration_pending": True,
            "formal_freeze_eligible": False,
            "candidate_execution_performed": True,
            "complete": True,
        },
    }
    inputs = {
        "version_materialization": (_SOURCE_CAPSULE, _REPACKAGE, _HUDFIX_RECEIPT),
        "frozen_replay": (
            _MAPPING,
            _SERVER_REPLAY,
            _SERVER_OBSERVATION,
            _HUDFIX_RECEIPT,
        ),
        "hidden_oracle": (
            _MAPPING,
            _SERVER_ORACLE,
            _HUDFIX_OBSERVATION,
            _HUDFIX_MATRIX,
            _HUDFIX_RECEIPT,
        ),
        "construction_gate": (_MAPPING, _HUDFIX_CONSTRUCTION, _HUDFIX_RECEIPT),
    }
    return {
        stage: _write_or_load(
            root,
            ref,
            inputs=inputs[stage],
            payload=payloads[stage],
            authorization=authorization,
        )
        for stage, ref in refs.items()
    }


def _write_or_load_admission_blocker(
    root: Path,
    *,
    authorization: AuthorizationRecord,
    mapping: StableArtifact,
    catalog_path: Path,
    workspace_root: Path,
) -> StableArtifact:
    task_id = _mapping_task_id(mapping)
    catalog_present = _catalog_contains(
        catalog_path=catalog_path, workspace_root=workspace_root, task_id=task_id
    )
    existing = _try_load(root, _BLOCKER)
    if catalog_present and existing is not None:
        # Preserve the immutable historical nonterminal blocker.  A successful
        # curated transition/admission supersedes it; rewriting its current head
        # after the catalog migration would erase the original stop reason.
        return existing
    payload = {
        "candidate": CANDIDATE.to_value(),
        "task_id": task_id,
        "state": "downstream_pending",
        "outcome": "infrastructure_blocked",
        "terminal": False,
        "resumable": True,
        "complete": True,
        "reason": (
            "curated_catalog_row_missing"
            if not catalog_present
            else "curated_legacy_lineage_missing"
        ),
        "catalog_path": catalog_path.as_posix(),
        "catalog_contains_candidate": catalog_present,
        "required_for_transition": [
            "catalog_task_row",
            "legacy_curated_selection",
            "three_fresh_curated_construction_repetitions",
            "curated_task_revalidation_aggregate",
        ],
        "schema_boundary": (
            "write_curated_construction_transition_receipt_v2 requires the "
            "catalog task plus a complete normal v2 eligibility chain and "
            "typed legacy revalidation lineage"
        ),
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    return _write_or_load(
        root,
        _BLOCKER,
        inputs=(mapping.reference,),
        payload=payload,
        authorization=authorization,
    )


def _catalog_contains(
    *, catalog_path: Path, workspace_root: Path, task_id: str
) -> bool:
    try:
        tasks = load_curated_catalog(
            catalog_path,
            workspace_root=workspace_root,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return False
    return any(task.task_id == task_id for task in tasks)


def _mapping_task_id(mapping: StableArtifact) -> str:
    task_id = mapping.payload.get("task_id")
    if type(task_id) is not str or not task_id:
        raise RuntimeError("#14757 mapping has no stable task ID")
    return task_id


def _require_executable_regular(root: Path, relative: object) -> None:
    if type(relative) is not str or not relative:
        raise RuntimeError("#14757 materialized application path is invalid")
    raw_path = root / relative
    if raw_path.is_symlink():
        raise RuntimeError("#14757 materialized application path escapes root")
    path = raw_path.resolve(strict=False)
    if not path.is_relative_to(root):
        raise RuntimeError("#14757 materialized application path escapes root")
    try:
        mode = path.stat().st_mode
    except OSError as error:
        raise RuntimeError(
            "#14757 materialized application binary is missing"
        ) from error
    if not stat.S_ISREG(mode) or not bool(mode & stat.S_IXUSR):
        raise RuntimeError("#14757 materialized application binary is not executable")


def _require_application_bundle(root: Path, relative: object) -> None:
    if type(relative) is not str or not relative:
        raise RuntimeError("#14757 materialized application path is invalid")
    raw_path = root / relative
    if raw_path.is_symlink():
        raise RuntimeError("#14757 materialized application path escapes root")
    path = raw_path.resolve(strict=False)
    if not path.is_relative_to(root) or not path.is_dir():
        raise RuntimeError("#14757 materialized application bundle is invalid")
    _require_executable_regular(
        root,
        (path / "Contents/MacOS/minetest").relative_to(root).as_posix(),
    )


def _write_or_load(
    root: Path,
    reference: ArtifactReference,
    *,
    inputs: tuple[ArtifactReference, ...],
    payload: dict[str, object],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    existing = _try_load(root, reference)
    if existing is not None:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError(f"{reference.artifact_id} current head conflicts")
        return existing
    write_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
        created_at=datetime.now(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z"),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    return _load(root, reference)


def _artifact_present(root: Path, reference: ArtifactReference) -> bool:
    try:
        _load(root, reference)
    except RuntimeError:
        return False
    return True


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


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


__all__ = [
    "CANDIDATE",
    "MainlineClosureResult",
    "integrate_luanti_14757_mainline",
]
