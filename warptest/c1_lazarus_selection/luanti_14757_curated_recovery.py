"""Recover Luanti #14757 into the 24-task curated catalog.

The recovery is non-executing.  It replaces the terminal #14586 catalog row,
binds the already fresh three-by-four Lua regression epoch into the historical
curated construction schema, and then invokes the normal v2 transition and
admission writers.  No repetition is duplicated: each curated repetition is
derived from four distinct typed attempt receipts with the same repetition
number.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .curated import (
    load_curated_catalog,
    write_curated_selection,
)
from .luanti_14757_mainline_closure import (
    CANDIDATE,
    MainlineClosureResult,
    integrate_luanti_14757_mainline,
)
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


TASK_ID = "lz-luanti-14757-math-round"
_SELECTION_ID = "curated_selection/all/current/v1"
_REVALIDATION_ID = "curated_task_revalidation/luanti/14757/construction"
_LUA_TRANSITION = ArtifactReference(
    "luanti_lua_regression_transition/luanti/14757/test-repetitions/v2",
    "luanti_lua_regression_transition",
    "2.0",
)
_MAPPING = ArtifactReference(
    "lazarus_task_mapping/luanti/14757/lz-luanti-14757-math-round/v4",
    "lazarus_task_mapping",
    "4.0",
)


@dataclass(frozen=True)
class CuratedRecoveryResult:
    audit: StableArtifact
    selection: StableArtifact
    repetitions: tuple[StableArtifact, ...]
    revalidation: StableArtifact
    mainline: MainlineClosureResult


def recover_luanti_14757_curated(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    strict_selection_artifact_id: str = "population_selection/all/current/v1",
    catalog_path: str | Path = "benchmarks/p5/lazarus/curated.yaml",
    workspace_root: str | Path = ".",
) -> CuratedRecoveryResult:
    """Materialize the catalog/lineage migration and normal v2 admission."""

    root = Path(artifact_root).absolute().resolve(strict=True)
    workspace = Path(workspace_root).absolute().resolve(strict=True)
    catalog = Path(catalog_path)
    tasks = load_curated_catalog(catalog, workspace_root=workspace)
    matches = [task for task in tasks if task.task_id == TASK_ID]
    if len(matches) != 1 or matches[0].change_number != 14757:
        raise RuntimeError("#14757 curated catalog row is missing or ambiguous")
    if any(task.change_number == 14586 for task in tasks):
        raise RuntimeError("terminal #14586 still occupies the curated catalog")

    mapping = _load(root, _MAPPING)
    transition = _load(root, _LUA_TRANSITION)
    attempts = _load_source_attempts(root, transition)
    audit = _write_old24_audit(
        root,
        mapping=mapping,
        transition=transition,
        authorization=authorization,
    )
    selection = write_curated_selection(
        root,
        catalog_path=catalog,
        workspace_root=workspace,
        strict_selection_artifact_id=strict_selection_artifact_id,
        authorization=authorization,
    )
    repetitions = tuple(
        _write_repetition(
            root,
            selection=selection,
            source_attempts=attempts[repetition],
            repetition=repetition,
            authorization=authorization,
        )
        for repetition in (1, 2, 3)
    )
    revalidation = _write_revalidation(
        root,
        selection=selection,
        repetitions=repetitions,
        transition=transition,
        authorization=authorization,
    )
    _write_eligibility_recovery(
        root,
        selection=selection,
        revalidation=revalidation,
        transition=transition,
        mapping=mapping,
        authorization=authorization,
    )
    mainline = integrate_luanti_14757_mainline(
        root,
        authorization=authorization,
        catalog_path=catalog,
        workspace_root=workspace,
    )
    if mainline.transition is None or mainline.admission is None:
        raise RuntimeError("#14757 normal v2 curated admission was not created")
    return CuratedRecoveryResult(
        audit=audit,
        selection=selection,
        repetitions=repetitions,
        revalidation=revalidation,
        mainline=mainline,
    )


def _load_source_attempts(
    root: Path, transition: StableArtifact
) -> dict[int, tuple[StableArtifact, ...]]:
    payload = transition.payload
    if (
        payload.get("candidate") != CANDIDATE.to_value()
        or payload.get("qualified") is not True
        or payload.get("complete") is not True
        or payload.get("attempt_count") != 12
        or payload.get("repetitions_per_cell") != 3
        or payload.get("stable_repetitions") is not True
        or payload.get("all_expected") is not True
        or payload.get("truth_table")
        != {
            "fixed": {"f2p": True, "p2p": True},
            "buggy": {"f2p": False, "p2p": True},
        }
    ):
        raise RuntimeError("#14757 fresh Lua transition is not qualified")

    result: dict[int, list[StableArtifact]] = {1: [], 2: [], 3: []}
    for repetition in (1, 2, 3):
        for phase in ("f2p", "p2p"):
            for variant in ("fixed", "buggy"):
                reference = ArtifactReference(
                    (
                        f"lua_regression_attempt/luanti/14757/{phase}/"
                        f"{variant}/{repetition}/v3"
                    ),
                    "lua_regression_attempt",
                    "3.0",
                )
                attempt = _load(root, reference)
                expected = (
                    "failure" if phase == "f2p" and variant == "buggy" else "pass"
                )
                if (
                    attempt.payload.get("candidate") != CANDIDATE.to_value()
                    or attempt.payload.get("repetition") != repetition
                    or attempt.payload.get("phase") != phase
                    or attempt.payload.get("variant") != variant
                    or attempt.payload.get("observed_outcome") != expected
                    or attempt.payload.get("matched_expected") is not True
                    or attempt.payload.get("process_outcome") != "completed"
                    or attempt.payload.get("completed_test_count") != 1
                ):
                    raise RuntimeError("#14757 source attempt truth table drifted")
                if reference not in transition.inputs:
                    raise RuntimeError(
                        "#14757 transition does not bind a source attempt"
                    )
                result[repetition].append(attempt)
    return {key: tuple(value) for key, value in result.items()}


def _write_old24_audit(
    root: Path,
    *,
    mapping: StableArtifact,
    transition: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    return _write_and_reload(
        root,
        ArtifactReference(f"old24_audit/{TASK_ID}/current/v1", "old24_audit", "1.0"),
        inputs=(mapping.reference, transition.reference),
        payload={
            "task_label": TASK_ID,
            "repository_key": "luanti",
            "change_number": 14757,
            "mapping_state": "mapped",
            "enumeration_outcome": "current_v2_qualified_replacement",
            "screening_state": "qualified",
            "screening_outcome": "gate_1_7_qualified",
            "inherited_conclusions": False,
            "supersedes_terminal_catalog_candidate": 14586,
        },
        authorization=authorization,
    )


def _write_repetition(
    root: Path,
    *,
    selection: StableArtifact,
    source_attempts: tuple[StableArtifact, ...],
    repetition: int,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    if len(source_attempts) != 4:
        raise RuntimeError("#14757 curated repetition needs four distinct cells")
    source_refs = [_reference_value(attempt.reference) for attempt in source_attempts]
    return _write_and_reload(
        root,
        ArtifactReference(
            f"{_REVALIDATION_ID}/repetition-{repetition}",
            "curated_construction_repetition",
            "1.0",
        ),
        # The legacy loader requires selection-only inputs.  The exact four
        # current-v3 source receipts remain hash-bound in the immutable payload.
        inputs=(selection.reference,),
        payload={
            "task_id": TASK_ID,
            "candidate": CANDIDATE.to_value(),
            "defect_locus": "at_goal",
            "repetition": repetition,
            "outcome": "qualified",
            "complete": True,
            "fresh_revalidation": True,
            "historical_result_inherited": False,
            "fixed_tests": {"f2p": True, "p2p": True},
            "buggy_tests": {"f2p": False, "p2p": True},
            "failures": [],
            "executions": source_refs,
            "source_attempts": source_refs,
            "source_attempt_count": 4,
            "source_epoch": "lua_regression_v3_fresh",
            "runner_error": None,
        },
        authorization=authorization,
    )


def _write_revalidation(
    root: Path,
    *,
    selection: StableArtifact,
    repetitions: tuple[StableArtifact, ...],
    transition: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    return _write_and_reload(
        root,
        ArtifactReference(_REVALIDATION_ID, "curated_task_revalidation", "2.0"),
        inputs=(selection.reference, *(item.reference for item in repetitions)),
        payload={
            "task_id": TASK_ID,
            "candidate": CANDIDATE.to_value(),
            "defect_locus": "at_goal",
            "outcome": "qualified",
            "complete": True,
            "qualified": True,
            "fresh_revalidation": True,
            "historical_result_inherited": False,
            "required_repetitions": 3,
            "completed_repetitions": 3,
            "fixed_tests": {"f2p": True, "p2p": True},
            "buggy_tests": {"f2p": False, "p2p": True},
            "source_transition": _reference_value(transition.reference),
            "source_attempt_count": 12,
            "repetitions": [
                {
                    "repetition": repetition,
                    "outcome": "qualified",
                    "complete": True,
                    "fixed_tests": {"f2p": True, "p2p": True},
                    "buggy_tests": {"f2p": False, "p2p": True},
                    "runner_error": None,
                }
                for repetition in (1, 2, 3)
            ],
        },
        authorization=authorization,
    )


def _write_eligibility_recovery(
    root: Path,
    *,
    selection: StableArtifact,
    revalidation: StableArtifact,
    transition: StableArtifact,
    mapping: StableArtifact,
    authorization: AuthorizationRecord,
) -> tuple[StableArtifact, StableArtifact, StableArtifact]:
    evidence = _write_and_reload(
        root,
        ArtifactReference(
            "candidate_curated_lineage_evidence/luanti/14757/current/v2",
            "candidate_curated_lineage_evidence",
            "2.0",
        ),
        inputs=(
            mapping.reference,
            transition.reference,
            selection.reference,
            revalidation.reference,
        ),
        payload={
            "candidate": CANDIDATE.to_value(),
            "task_id": TASK_ID,
            "state": "passed",
            "outcome": "catalog_and_three_repetitions_recovered",
            "catalog_task_count": 24,
            "required_repetitions": 3,
            "completed_repetitions": 3,
            "source_attempt_count": 12,
            "historical_terminal_revision_preserved": True,
            "candidate_execution_performed": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
            "complete": True,
        },
        authorization=authorization,
    )
    predecessor = _load(
        root,
        ArtifactReference(
            "candidate_stage/luanti/14757/construction_gate/v2",
            "candidate_stage",
            "2.0",
        ),
    )
    stage = _write_and_reload(
        root,
        ArtifactReference(
            "candidate_stage/luanti/14757/annotation_eligibility/v2",
            "candidate_stage",
            "2.0",
        ),
        inputs=(predecessor.reference, evidence.reference),
        payload={
            "candidate": CANDIDATE.to_value(),
            "stage": "annotation_eligibility",
            "state": "eligible",
            "outcome": "qualified",
            "complete": True,
            "details": {
                "evidence_artifacts": [_reference_value(evidence.reference)],
                "evidence_status": "curated_catalog_lineage_recovered",
                "candidate_execution_performed": False,
            },
        },
        authorization=authorization,
    )
    checkpoint = _write_and_reload(
        root,
        ArtifactReference(
            "preannotation_checkpoint/luanti/14757/current/v2",
            "preannotation_checkpoint",
            "2.0",
        ),
        inputs=(stage.reference,),
        payload={
            "candidate": CANDIDATE.to_value(),
            "completed_stages": [
                "test_manifest",
                "test_repetitions",
                "version_materialization",
                "frozen_replay",
                "hidden_oracle",
                "construction_gate",
                "annotation_eligibility",
            ],
            "state": "eligible",
            "outcome": "qualified",
            "current_artifact_id": stage.reference.artifact_id,
        },
        authorization=authorization,
    )
    return evidence, stage, checkpoint


def _write_and_reload(
    root: Path,
    reference: ArtifactReference,
    *,
    inputs: tuple[ArtifactReference, ...],
    payload: dict[str, object],
    authorization: AuthorizationRecord,
) -> StableArtifact:
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
    artifact = _load(root, reference)
    if artifact.inputs != inputs or dict(artifact.payload) != payload:
        raise RuntimeError(f"{reference.artifact_id} reload drift")
    return artifact


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _reference_value(reference: ArtifactReference) -> dict[str, str]:
    return {
        "artifact_id": reference.artifact_id,
        "artifact_kind": reference.artifact_kind,
        "semantic_version": reference.semantic_version,
    }


__all__ = ["CuratedRecoveryResult", "recover_luanti_14757_curated"]
