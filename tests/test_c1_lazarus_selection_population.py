from __future__ import annotations

import os
from pathlib import Path

from warptest.c1_lazarus_selection.enumeration import (
    RepositoryEnumeration,
    write_enumeration_audit_population,
    write_repository_enumeration,
)
from warptest.c1_lazarus_selection.models import CandidateKey
from warptest.c1_lazarus_selection.population import (
    write_complete_selection_coverage,
    write_screening_population,
)
from warptest.c1_lazarus_selection.selection import (
    write_population_selection,
    write_sampling_power_report,
)
from warptest.c1_lazarus_selection.stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    load_stable_artifact,
    write_stable_artifact,
)


def _authorization() -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id="local-population/test",
        scope="candidate-screening",
        granted_at="2026-08-13T00:00:00Z",
        local_user_id=os.getuid(),
    )


def _screening_checkpoint(root: Path, candidate: CandidateKey, outcome: str) -> None:
    stage = ArtifactReference(
        f"candidate_terminal/{candidate.repository_key}/{candidate.change_number}/v1",
        "candidate_terminal",
        "1.0",
    )
    write_stable_artifact(
        root,
        artifact_id=stage.artifact_id,
        artifact_kind=stage.artifact_kind,
        semantic_version=stage.semantic_version,
        created_at="2026-08-13T00:00:01Z",
        inputs=(),
        payload={"candidate": candidate.to_value(), "complete": True},
        authorization=_authorization(),
    )
    write_stable_artifact(
        root,
        artifact_id=(
            f"screening_checkpoint/{candidate.repository_key}/"
            f"{candidate.change_number}/current/v1"
        ),
        artifact_kind="screening_checkpoint",
        semantic_version="1.0",
        created_at="2026-08-13T00:00:02Z",
        inputs=(stage,),
        payload={
            "candidate": candidate.to_value(),
            "state": "complete",
            "outcome": outcome,
            "stage_artifact_id": stage.artifact_id,
        },
        authorization=_authorization(),
    )


def test_complete_population_binds_all_deep_and_enumeration_terminals(
    tmp_path: Path,
) -> None:
    luanti = RepositoryEnumeration(
        repository_key="luanti",
        merged_change_numbers=(1, 2),
        authoritative_issue_link_missing=(1,),
        repair_not_in_pinned=(),
        candidates=(CandidateKey("luanti", 2),),
    )
    jupyter = RepositoryEnumeration(
        repository_key="jupyterlab",
        merged_change_numbers=(3,),
        authoritative_issue_link_missing=(),
        repair_not_in_pinned=(),
        candidates=(CandidateKey("jupyterlab", 3),),
    )
    for enumeration in (luanti, jupyter):
        write_repository_enumeration(
            tmp_path, enumeration, authorization=_authorization()
        )
        write_enumeration_audit_population(
            tmp_path, enumeration, authorization=_authorization()
        )
    _screening_checkpoint(tmp_path, CandidateKey("luanti", 2), "source_rejected")
    _screening_checkpoint(tmp_path, CandidateKey("jupyterlab", 3), "patch_eligible")
    screening = load_stable_artifact(
        tmp_path,
        artifact_id="screening_checkpoint/jupyterlab/3/current/v1",
        artifact_kind="screening_checkpoint",
        semantic_version="1.0",
    )
    write_stable_artifact(
        tmp_path,
        artifact_id="preannotation_checkpoint/jupyterlab/3/current/v1",
        artifact_kind="preannotation_checkpoint",
        semantic_version="1.0",
        created_at="2026-08-13T00:00:03Z",
        inputs=(screening.reference,),
        payload={
            "candidate": {
                "repository_key": "jupyterlab",
                "change_number": 3,
            },
            "completed_stages": ["test_manifest"],
            "state": "terminal",
            "outcome": "quality_rejected",
            "current_artifact_id": screening.reference.artifact_id,
        },
        authorization=_authorization(),
    )

    population = write_screening_population(
        tmp_path,
        repository_order=("luanti", "jupyterlab"),
        authorization=_authorization(),
    )
    coverage = write_complete_selection_coverage(
        tmp_path,
        repository_order=("luanti", "jupyterlab"),
        expected_merged_count=3,
        authorization=_authorization(),
    )

    assert population.deep_screen_count == 2
    assert population.outcome_counts == {
        "quality_rejected": 1,
        "source_rejected": 1,
    }
    assert coverage.payload["merged_count"] == 3
    assert coverage.payload["enumeration_terminal_count"] == 1
    assert coverage.payload["deep_screen_count"] == 2
    assert coverage.payload["complete"] is True
    assert len(coverage.inputs) == 7

    selection = write_population_selection(
        tmp_path,
        screening_population_artifact_id="screening_population/all/current/v1",
        coverage_artifact_id="selection_coverage/all/current/v1",
        authorization=_authorization(),
    )
    power = write_sampling_power_report(
        tmp_path,
        population_selection_artifact_id=selection.reference.artifact_id,
        authorization=_authorization(),
    )
    assert selection.payload["qualified_count"] == 0
    assert selection.payload["selected_candidates"] == []
    assert selection.payload["selection_status"] == "insufficient_population"
    assert selection.payload["paid_actions_authorized"] is False
    assert power.payload["stop_status"] == "insufficient_population"
    assert power.payload["confirmatory_execution_allowed"] is False
