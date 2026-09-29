"""V2 population and curated-admission epoch boundaries."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from warptest.c1_lazarus_selection.curated import (
    load_curated_catalog,
    write_curated_construction_transition_receipt_v2,
    write_curated_selection_v2,
    write_curated_task_admission_v2,
)
from warptest.c1_lazarus_selection.epoch import SELECTION_EPOCH_V2
from warptest.c1_lazarus_selection.models import CandidateKey
from warptest.c1_lazarus_selection.population import (
    load_v2_candidate_eligibility,
    write_complete_selection_coverage_v2,
    write_screening_population_v2,
)
from warptest.c1_lazarus_selection.stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    load_stable_artifact,
    write_stable_artifact,
)


WORKSPACE = Path(__file__).resolve().parents[1]
CATALOG = WORKSPACE / "benchmarks/p5/lazarus/curated.yaml"


def test_v2_population_and_coverage_never_fall_back_to_v1(tmp_path: Path) -> None:
    authorization = _authorization()
    _write_v2_enumeration(tmp_path, "luanti", (7,), authorization)
    _write_v1_source_terminal(tmp_path, "luanti", 7, authorization)

    with pytest.raises(RuntimeError, match="v2 screening checkpoint"):
        write_screening_population_v2(
            tmp_path,
            repository_order=("luanti",),
            authorization=authorization,
        )

    enum = _v2_enumeration_reference("luanti")
    source = _write_artifact(
        tmp_path,
        artifact_id="candidate_source_terminal/luanti/7/source/v2",
        artifact_kind="candidate_source_terminal",
        inputs=(enum,),
        payload={
            "candidate": _candidate("luanti", 7),
            "outcome": "source_rejected",
            "reason": "authoritative_issue_link_missing",
            "complete": True,
        },
        authorization=authorization,
    )
    checkpoint = _write_artifact(
        tmp_path,
        artifact_id="screening_checkpoint/luanti/7/current/v2",
        artifact_kind="screening_checkpoint",
        inputs=(enum, source.reference),
        payload={
            "candidate": _candidate("luanti", 7),
            "state": "complete",
            "outcome": "source_rejected",
            "stage_artifact_id": source.reference.artifact_id,
        },
        authorization=authorization,
    )
    population = write_screening_population_v2(
        tmp_path,
        repository_order=("luanti",),
        authorization=authorization,
    )
    assert population.artifact.reference == ArtifactReference(
        "screening_population/all/current/v2", "screening_population", "2.0"
    )
    assert population.artifact.inputs == (enum, checkpoint.reference)

    _write_artifact(
        tmp_path,
        artifact_id="enumeration_audit/luanti/7/current/v1",
        artifact_kind="enumeration_audit",
        semantic_version="1.0",
        payload={"candidate": _candidate("luanti", 7), "complete": True},
        authorization=authorization,
    )
    with pytest.raises(RuntimeError, match="v2 enumeration audit"):
        write_complete_selection_coverage_v2(
            tmp_path,
            repository_order=("luanti",),
            expected_merged_count=1,
            authorization=authorization,
        )

    audit = _write_artifact(
        tmp_path,
        artifact_id="enumeration_audit/luanti/7/current/v2",
        artifact_kind="enumeration_audit",
        inputs=(enum,),
        payload={"candidate": _candidate("luanti", 7), "complete": True},
        authorization=authorization,
    )
    coverage = write_complete_selection_coverage_v2(
        tmp_path,
        repository_order=("luanti",),
        expected_merged_count=1,
        authorization=authorization,
    )
    assert coverage.reference == ArtifactReference(
        "selection_coverage/all/current/v2", "selection_coverage", "2.0"
    )
    assert coverage.inputs == (enum, population.artifact.reference, audit.reference)


def test_v2_coverage_binds_infrastructure_enumeration_terminal(
    tmp_path: Path,
) -> None:
    authorization = _authorization()
    _write_v2_enumeration(
        tmp_path,
        "luanti",
        (7,),
        authorization,
        infrastructure_blocked=(8,),
    )
    enum = _v2_enumeration_reference("luanti")
    source = _write_artifact(
        tmp_path,
        artifact_id="candidate_source_terminal/luanti/7/source/v2",
        artifact_kind="candidate_source_terminal",
        inputs=(enum,),
        payload={
            "candidate": _candidate("luanti", 7),
            "outcome": "source_rejected",
            "reason": "authoritative_issue_link_missing",
            "complete": True,
        },
        authorization=authorization,
    )
    checkpoint = _write_artifact(
        tmp_path,
        artifact_id="screening_checkpoint/luanti/7/current/v2",
        artifact_kind="screening_checkpoint",
        inputs=(enum, source.reference),
        payload={
            "candidate": _candidate("luanti", 7),
            "state": "complete",
            "outcome": "source_rejected",
            "stage_artifact_id": source.reference.artifact_id,
        },
        authorization=authorization,
    )
    for number in (7, 8):
        _write_artifact(
            tmp_path,
            artifact_id=f"enumeration_audit/luanti/{number}/current/v2",
            artifact_kind="enumeration_audit",
            inputs=(enum,),
            payload={"candidate": _candidate("luanti", number), "complete": True},
            authorization=authorization,
        )
    terminal = _write_artifact(
        tmp_path,
        artifact_id="enumeration_terminal/luanti/8/infrastructure-blocked/v2",
        artifact_kind="enumeration_terminal",
        inputs=(enum,),
        payload={
            "candidate": _candidate("luanti", 8),
            "reason": "infrastructure_blocked",
            "complete": True,
        },
        authorization=authorization,
    )
    population = write_screening_population_v2(
        tmp_path,
        repository_order=("luanti",),
        authorization=authorization,
    )
    coverage = write_complete_selection_coverage_v2(
        tmp_path,
        repository_order=("luanti",),
        expected_merged_count=2,
        authorization=authorization,
    )

    assert population.artifact.inputs == (enum, checkpoint.reference)
    assert population.deep_screen_count == 1
    assert coverage.payload["merged_count"] == 2
    assert coverage.payload["deep_screen_count"] == 1
    assert coverage.payload["enumeration_terminal_count"] == 1
    assert terminal.reference in coverage.inputs


def test_v2_transition_refuses_direct_reuse_without_v2_candidate_chain(
    tmp_path: Path,
) -> None:
    authorization = _authorization()
    task = load_curated_catalog(CATALOG, workspace_root=WORKSPACE)[0]
    _write_v2_enumeration(
        tmp_path,
        task.repository_key,
        (task.change_number,),
        authorization,
    )
    original_selection, original_revalidation = _write_legacy_revalidation(
        tmp_path, task, authorization
    )

    with pytest.raises(RuntimeError, match="v2 candidate source packet"):
        write_curated_construction_transition_receipt_v2(
            tmp_path,
            task_id=task.task_id,
            catalog_path=CATALOG,
            workspace_root=WORKSPACE,
            original_selection_artifact_id=original_selection.reference.artifact_id,
            original_revalidation_artifact_id=original_revalidation.reference.artifact_id,
            authorization=authorization,
        )


def test_v2_transition_requires_exact_identity_repetitions_and_normal_chain(
    tmp_path: Path,
) -> None:
    authorization = _authorization()
    task = load_curated_catalog(CATALOG, workspace_root=WORKSPACE)[0]
    _write_v2_enumeration(
        tmp_path,
        task.repository_key,
        (task.change_number,),
        authorization,
    )
    original_selection, original_revalidation = _write_legacy_revalidation(
        tmp_path,
        task,
        authorization,
        buggy_p2p=False,
    )
    _write_v2_eligible_chain(tmp_path, task, authorization)

    with pytest.raises(RuntimeError, match="truth table"):
        write_curated_construction_transition_receipt_v2(
            tmp_path,
            task_id=task.task_id,
            catalog_path=CATALOG,
            workspace_root=WORKSPACE,
            original_selection_artifact_id=original_selection.reference.artifact_id,
            original_revalidation_artifact_id=original_revalidation.reference.artifact_id,
            authorization=authorization,
        )


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("source_manifest_path", "raw/tampered/source.json"),
        ("raw_patch_path", "raw/tampered/patch.diff"),
    ),
)
def test_v2_eligibility_requires_canonical_raw_source_to_patch_paths(
    tmp_path: Path,
    field: str,
    replacement: str,
) -> None:
    authorization = _authorization()
    task = load_curated_catalog(CATALOG, workspace_root=WORKSPACE)[0]
    _write_v2_enumeration(
        tmp_path,
        task.repository_key,
        (task.change_number,),
        authorization,
    )
    _write_v2_eligible_chain(tmp_path, task, authorization)

    patch_id = (
        f"candidate_test_patch/{task.repository_key}/{task.change_number}/screening/v2"
    )
    patch = load_stable_artifact(
        tmp_path,
        artifact_id=patch_id,
        artifact_kind="candidate_test_patch",
        semantic_version="2.0",
    )
    _write_artifact(
        tmp_path,
        artifact_id=patch_id,
        artifact_kind="candidate_test_patch",
        inputs=patch.inputs,
        payload={**patch.payload, field: replacement},
        authorization=authorization,
        created_at="2026-08-13T02:00:01Z",
    )

    with pytest.raises(RuntimeError, match="raw evidence binding"):
        load_v2_candidate_eligibility(
            tmp_path,
            candidate=CandidateKey(task.repository_key, task.change_number),
        )


def test_v2_transition_rejects_aggregate_that_detaches_from_attempt_table(
    tmp_path: Path,
) -> None:
    authorization = _authorization()
    task = load_curated_catalog(CATALOG, workspace_root=WORKSPACE)[0]
    _write_v2_enumeration(
        tmp_path,
        task.repository_key,
        (task.change_number,),
        authorization,
    )
    _write_v2_eligible_chain(tmp_path, task, authorization)
    selection, revalidation = _write_legacy_revalidation(tmp_path, task, authorization)
    _write_artifact(
        tmp_path,
        artifact_id=revalidation.reference.artifact_id,
        artifact_kind=revalidation.reference.artifact_kind,
        inputs=revalidation.inputs,
        payload={
            **revalidation.payload,
            "fixed_tests": {"f2p": True, "p2p": False},
        },
        authorization=authorization,
        created_at="2026-08-13T02:00:01Z",
    )

    with pytest.raises(RuntimeError, match="revalidation binding"):
        write_curated_construction_transition_receipt_v2(
            tmp_path,
            task_id=task.task_id,
            catalog_path=CATALOG,
            workspace_root=WORKSPACE,
            original_selection_artifact_id=selection.reference.artifact_id,
            original_revalidation_artifact_id=revalidation.reference.artifact_id,
            authorization=authorization,
        )


def test_v2_transition_rejects_non_v2_or_dangling_stage_evidence(
    tmp_path: Path,
) -> None:
    authorization = _authorization()
    task = load_curated_catalog(CATALOG, workspace_root=WORKSPACE)[0]
    _write_v2_enumeration(
        tmp_path,
        task.repository_key,
        (task.change_number,),
        authorization,
    )
    _write_v2_eligible_chain(
        tmp_path,
        task,
        authorization,
        stage_evidence=ArtifactReference(
            "legacy-stage-evidence/all/current/v1", "legacy_stage_evidence", "1.0"
        ),
    )
    selection, revalidation = _write_legacy_revalidation(tmp_path, task, authorization)

    with pytest.raises(RuntimeError, match="evidence is not v2"):
        write_curated_construction_transition_receipt_v2(
            tmp_path,
            task_id=task.task_id,
            catalog_path=CATALOG,
            workspace_root=WORKSPACE,
            original_selection_artifact_id=selection.reference.artifact_id,
            original_revalidation_artifact_id=revalidation.reference.artifact_id,
            authorization=authorization,
        )


def test_v2_curated_selection_requires_v2_population_coverage_audits_and_admissions(
    tmp_path: Path,
) -> None:
    authorization = _authorization()
    tasks = load_curated_catalog(CATALOG, workspace_root=WORKSPACE)
    _write_v2_selection_fixture(tmp_path, tasks, authorization)

    selected = write_curated_selection_v2(
        tmp_path,
        catalog_path=CATALOG,
        workspace_root=WORKSPACE,
        screening_population_artifact_id="screening_population/all/current/v2",
        coverage_artifact_id="selection_coverage/all/current/v2",
        authorization=authorization,
    )

    assert selected.reference == ArtifactReference(
        "curated_selection/all/current/v2",
        "curated_selection",
        "2.0",
    )
    assert selected.payload["selection_mode"] == "curated_v2"
    assert selected.payload["selected_task_count"] == 24
    assert selected.payload["paid_actions_authorized"] is False
    assert all(
        "strict_audit_artifact_id" not in task for task in selected.payload["tasks"]
    )
    assert all(reference.semantic_version == "2.0" for reference in selected.inputs)


@pytest.mark.parametrize("mapping_state", ("mapped", "unresolved"))
def test_v2_curated_selection_ignores_mapped_or_unresolved_old24_labels(
    tmp_path: Path,
    mapping_state: str,
) -> None:
    authorization = _authorization()
    task = load_curated_catalog(CATALOG, workspace_root=WORKSPACE)[0]
    _write_v2_selection_fixture(
        tmp_path,
        load_curated_catalog(CATALOG, workspace_root=WORKSPACE),
        authorization,
        skip_admission_task_id=task.task_id,
    )
    old_payload = {
        "task_label": task.task_id,
        "repository_key": task.repository_key,
        "mapping_state": mapping_state,
        "inherited_conclusions": False,
    }
    if mapping_state == "mapped":
        old_payload["change_number"] = task.change_number
    else:
        old_payload.update(
            {
                "change_number": None,
                "reason": "change_number_missing",
            }
        )
    _write_artifact(
        tmp_path,
        artifact_id=f"old24_audit/{task.task_id}/current/v1",
        artifact_kind="old24_audit",
        semantic_version="1.0",
        payload=old_payload,
        authorization=authorization,
    )

    with pytest.raises(RuntimeError, match="v2 curated task admission"):
        write_curated_selection_v2(
            tmp_path,
            catalog_path=CATALOG,
            workspace_root=WORKSPACE,
            screening_population_artifact_id="screening_population/all/current/v2",
            coverage_artifact_id="selection_coverage/all/current/v2",
            authorization=authorization,
        )


def _write_v2_selection_fixture(
    root: Path,
    tasks: tuple[object, ...],
    authorization: AuthorizationRecord,
    skip_admission_task_id: str | None = None,
) -> None:
    by_repository: dict[str, list[int]] = {}
    for task in tasks:
        by_repository.setdefault(task.repository_key, []).append(task.change_number)
    for repository_key, numbers in by_repository.items():
        _write_v2_enumeration(
            root, repository_key, tuple(sorted(numbers)), authorization
        )
    for task in tasks:
        _write_v2_eligible_chain(root, task, authorization)
    population = write_screening_population_v2(
        root,
        repository_order=tuple(by_repository),
        authorization=authorization,
    )
    write_complete_selection_coverage_v2(
        root,
        repository_order=tuple(by_repository),
        expected_merged_count=len(tasks),
        authorization=authorization,
    )
    legacy_selection = _write_legacy_selection(root, tasks, authorization)
    for task in tasks:
        selection, revalidation = _write_legacy_revalidation(
            root,
            task,
            authorization,
            selection=legacy_selection,
        )
        transition = write_curated_construction_transition_receipt_v2(
            root,
            task_id=task.task_id,
            catalog_path=CATALOG,
            workspace_root=WORKSPACE,
            original_selection_artifact_id=selection.reference.artifact_id,
            original_revalidation_artifact_id=revalidation.reference.artifact_id,
            authorization=authorization,
        )
        if task.task_id != skip_admission_task_id:
            admission = write_curated_task_admission_v2(
                root,
                task_id=task.task_id,
                catalog_path=CATALOG,
                workspace_root=WORKSPACE,
                transition_artifact_id=transition.reference.artifact_id,
                authorization=authorization,
            )
            assert admission.payload["task_id"] == task.task_id
    assert population.artifact.payload["selection_epoch"] == "v2"


def _write_v2_enumeration(
    root: Path,
    repository_key: str,
    numbers: tuple[int, ...],
    authorization: AuthorizationRecord,
    infrastructure_blocked: tuple[int, ...] = (),
) -> None:
    _write_artifact(
        root,
        artifact_id=f"repository_enumeration/{repository_key}/current/v2",
        artifact_kind="repository_enumeration",
        payload={
            "repository_key": repository_key,
            "merged_change_numbers": list((*numbers, *infrastructure_blocked)),
            "authoritative_issue_link_missing": [],
            "repair_not_in_pinned": [],
            "infrastructure_blocked": list(infrastructure_blocked),
            "candidates": [_candidate(repository_key, number) for number in numbers],
            "complete": True,
        },
        authorization=authorization,
    )


def _write_v2_eligible_chain(
    root: Path,
    task: object,
    authorization: AuthorizationRecord,
    stage_evidence: ArtifactReference | None = None,
) -> None:
    repository_key = task.repository_key
    change_number = task.change_number
    candidate = _candidate(repository_key, change_number)
    enumeration = _v2_enumeration_reference(repository_key)
    source_manifest_path = f"raw/{repository_key}/{change_number}.json"
    raw_patch_path = f"raw/{repository_key}/{change_number}.diff"
    patch_path = root / raw_patch_path
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    patch_path.write_bytes(b"diff --git a/src/main b/src/main\n")
    manifest_path = root / source_manifest_path
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "candidate": candidate,
                "raw_paths": [raw_patch_path],
                "patch_raw_path": raw_patch_path,
                "complete": True,
            }
        ),
        encoding="utf-8",
    )
    audit = _write_artifact(
        root,
        artifact_id=(f"enumeration_audit/{repository_key}/{change_number}/current/v2"),
        artifact_kind="enumeration_audit",
        inputs=(enumeration,),
        payload={"candidate": candidate, "complete": True},
        authorization=authorization,
    )
    source = _write_artifact(
        root,
        artifact_id=(
            f"candidate_source_packet/{repository_key}/{change_number}/source/v2"
        ),
        artifact_kind="candidate_source_packet",
        inputs=(enumeration,),
        payload={
            "candidate": candidate,
            "raw_evidence_path": source_manifest_path,
            "complete": True,
        },
        authorization=authorization,
    )
    patch = _write_artifact(
        root,
        artifact_id=(
            f"candidate_test_patch/{repository_key}/{change_number}/screening/v2"
        ),
        artifact_kind="candidate_test_patch",
        inputs=(enumeration, source.reference),
        payload={
            "candidate": candidate,
            "outcome": "patch_eligible",
            "production_paths": ["src/main"],
            "test_paths": ["tests/main"],
            "source_manifest_path": source_manifest_path,
            "raw_patch_path": raw_patch_path,
            "complete": True,
        },
        authorization=authorization,
    )
    checkpoint = _write_artifact(
        root,
        artifact_id=(
            f"screening_checkpoint/{repository_key}/{change_number}/current/v2"
        ),
        artifact_kind="screening_checkpoint",
        inputs=(enumeration, patch.reference),
        payload={
            "candidate": candidate,
            "state": "complete",
            "outcome": "patch_eligible",
            "stage_artifact_id": patch.reference.artifact_id,
        },
        authorization=authorization,
    )
    previous = patch.reference
    stages = (
        "test_manifest",
        "test_repetitions",
        "version_materialization",
        "frozen_replay",
        "hidden_oracle",
        "construction_gate",
        "annotation_eligibility",
    )
    for index, stage in enumerate(stages):
        eligible = index == len(stages) - 1
        inputs = (previous,)
        if stage_evidence is not None and index == 0:
            inputs = (previous, stage_evidence)
        artifact = _write_artifact(
            root,
            artifact_id=(
                f"candidate_stage/{repository_key}/{change_number}/{stage}/v2"
            ),
            artifact_kind="candidate_stage",
            inputs=inputs,
            payload={
                "candidate": candidate,
                "stage": stage,
                "state": "eligible" if eligible else "passed",
                "outcome": "qualified" if eligible else None,
                "details": {},
                "complete": True,
            },
            authorization=authorization,
        )
        previous = artifact.reference
    _write_artifact(
        root,
        artifact_id=(
            f"preannotation_checkpoint/{repository_key}/{change_number}/current/v2"
        ),
        artifact_kind="preannotation_checkpoint",
        inputs=(previous,),
        payload={
            "candidate": candidate,
            "completed_stages": list(stages),
            "state": "eligible",
            "outcome": "qualified",
            "current_artifact_id": previous.artifact_id,
        },
        authorization=authorization,
    )
    assert audit.reference.semantic_version == "2.0"
    assert checkpoint.reference.semantic_version == "2.0"


def _write_legacy_revalidation(
    root: Path,
    task: object,
    authorization: AuthorizationRecord,
    *,
    buggy_p2p: bool = True,
    selection: object | None = None,
):
    if selection is None:
        selection = _write_legacy_selection(root, (task,), authorization)
    attempts = []
    for repetition in (1, 2, 3):
        attempt = _write_artifact(
            root,
            artifact_id=(
                f"curated_task_revalidation/{task.repository_key}/"
                f"{task.change_number}/construction/repetition-{repetition}"
            ),
            artifact_kind="curated_construction_repetition",
            semantic_version="1.0",
            inputs=(selection.reference,),
            payload={
                "task_id": task.task_id,
                "candidate": _candidate(task.repository_key, task.change_number),
                "defect_locus": task.defect_locus,
                "repetition": repetition,
                "outcome": "qualified",
                "complete": True,
                "fresh_revalidation": True,
                "historical_result_inherited": False,
                "fixed_tests": {"f2p": True, "p2p": True},
                "buggy_tests": {"f2p": False, "p2p": buggy_p2p},
                "failures": [],
                "executions": [],
                "runner_error": None,
            },
            authorization=authorization,
        )
        attempts.append(attempt)
    return selection, _write_artifact(
        root,
        artifact_id=(
            f"curated_task_revalidation/{task.repository_key}/"
            f"{task.change_number}/construction"
        ),
        artifact_kind="curated_task_revalidation",
        semantic_version="2.0",
        inputs=(selection.reference, *(attempt.reference for attempt in attempts)),
        payload={
            "task_id": task.task_id,
            "candidate": _candidate(task.repository_key, task.change_number),
            "defect_locus": task.defect_locus,
            "outcome": "qualified",
            "complete": True,
            "qualified": True,
            "fresh_revalidation": True,
            "historical_result_inherited": False,
            "required_repetitions": 3,
            "completed_repetitions": 3,
            "fixed_tests": {"f2p": True, "p2p": True},
            "buggy_tests": {"f2p": False, "p2p": buggy_p2p},
            "repetitions": [
                {
                    "repetition": repetition,
                    "outcome": "qualified",
                    "complete": True,
                    "fixed_tests": {"f2p": True, "p2p": True},
                    "buggy_tests": {"f2p": False, "p2p": buggy_p2p},
                    "runner_error": None,
                }
                for repetition in (1, 2, 3)
            ],
        },
        authorization=authorization,
    )


def _write_legacy_selection(
    root: Path,
    tasks: tuple[object, ...],
    authorization: AuthorizationRecord,
):
    return _write_artifact(
        root,
        artifact_id="curated_selection/all/current/v1",
        artifact_kind="curated_selection",
        semantic_version="1.0",
        payload={
            "selection_mode": "curated_v1",
            "tasks": [
                {
                    "task_id": task.task_id,
                    "candidate": _candidate(task.repository_key, task.change_number),
                    "defect_locus": task.defect_locus,
                }
                for task in tasks
            ],
            "complete": True,
        },
        authorization=authorization,
    )


def _write_v1_source_terminal(
    root: Path,
    repository_key: str,
    change_number: int,
    authorization: AuthorizationRecord,
) -> None:
    _write_artifact(
        root,
        artifact_id=(
            f"candidate_source_terminal/{repository_key}/{change_number}/source/v1"
        ),
        artifact_kind="candidate_source_terminal",
        semantic_version="1.0",
        payload={
            "candidate": _candidate(repository_key, change_number),
            "outcome": "source_rejected",
            "reason": "old_only",
            "complete": True,
        },
        authorization=authorization,
    )


def _write_artifact(
    root: Path,
    *,
    artifact_id: str,
    artifact_kind: str,
    payload: dict[str, object],
    authorization: AuthorizationRecord,
    inputs: tuple[ArtifactReference, ...] = (),
    semantic_version: str = "2.0",
    created_at: str = "2026-08-13T02:00:00Z",
):
    write_stable_artifact(
        root,
        artifact_id=artifact_id,
        artifact_kind=artifact_kind,
        semantic_version=semantic_version,
        created_at=created_at,
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    return load_stable_artifact(
        root,
        artifact_id=artifact_id,
        artifact_kind=artifact_kind,
        semantic_version=semantic_version,
    )


def _authorization() -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id="local-epoch-v2-population/test",
        scope="candidate-screening",
        granted_at="2026-08-13T02:00:00Z",
        local_user_id=os.getuid(),
    )


def _candidate(repository_key: str, change_number: int) -> dict[str, object]:
    return {"repository_key": repository_key, "change_number": change_number}


def _v2_enumeration_reference(repository_key: str) -> ArtifactReference:
    return SELECTION_EPOCH_V2.enumeration_reference(repository_key)
