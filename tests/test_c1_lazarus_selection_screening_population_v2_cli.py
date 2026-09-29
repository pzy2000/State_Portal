"""Tests for the final, offline v2 screening-population aggregation CLI."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from types import ModuleType

import pytest

from warptest.c1_lazarus_selection.enumeration import (
    RepositoryEnumeration,
    write_enumeration_audit_population,
    write_repository_enumeration,
)
from warptest.c1_lazarus_selection.epoch import SELECTION_EPOCH_V2
from warptest.c1_lazarus_selection.models import CandidateKey
from warptest.c1_lazarus_selection.screening import (
    SourceDecision,
    run_screening_batch,
)
from warptest.c1_lazarus_selection.stable_protocol import (
    AuthorizationRecord,
    load_stable_artifact,
    write_stable_artifact,
)


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "aggregate_c1_lazarus_screening_population_v2.py"
REPOSITORY_ORDER = ("luanti", "blender", "jupyterlab", "openra", "libreoffice")


def test_final_v2_aggregation_requires_all_heads_and_closes_population(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_module()
    root = tmp_path / "selection"
    authorization = _authorization()
    _write_v2_enumeration_population(root, authorization)
    _write_v2_screening_terminal(root, CandidateKey("luanti", 10), authorization)
    _write_v2_screening_terminal(root, CandidateKey("jupyterlab", 30), authorization)

    assert (
        module.main(
            [
                "--artifact-root",
                str(root),
                "--authorization-id",
                "local-aggregation/final-v2",
                "--execute",
            ]
        )
        == 0
    )

    result = json.loads(capsys.readouterr().out)
    assert result == {
        "deep_screen_count": 2,
        "merged_count": 5,
        "outcome_counts": {"source_rejected": 2},
        "repository_order": list(REPOSITORY_ORDER),
        "screening_population_artifact_id": "screening_population/all/current/v2",
        "selection_coverage_artifact_id": "selection_coverage/all/current/v2",
        "selection_epoch": "v2",
    }
    population = load_stable_artifact(
        root,
        artifact_id="screening_population/all/current/v2",
        artifact_kind="screening_population",
        semantic_version="2.0",
    )
    assert population.payload["records"] == [
        {
            "candidate": {"repository_key": "luanti", "change_number": 10},
            "outcome": "source_rejected",
            "terminal_artifact_id": "screening_checkpoint/luanti/10/current/v2",
            "terminal_artifact_kind": "screening_checkpoint",
        },
        {
            "candidate": {"repository_key": "jupyterlab", "change_number": 30},
            "outcome": "source_rejected",
            "terminal_artifact_id": "screening_checkpoint/jupyterlab/30/current/v2",
            "terminal_artifact_kind": "screening_checkpoint",
        },
    ]
    coverage = load_stable_artifact(
        root,
        artifact_id="selection_coverage/all/current/v2",
        artifact_kind="selection_coverage",
        semantic_version="2.0",
    )
    assert coverage.payload["merged_count"] == 5
    assert coverage.payload["audit_count"] == 5
    assert coverage.payload["enumeration_terminal_count"] == 3


def test_final_v2_aggregation_never_uses_a_v1_screening_checkpoint(
    tmp_path: Path,
) -> None:
    module = _load_module()
    root = tmp_path / "selection"
    authorization = _authorization()
    _write_v2_enumeration_population(root, authorization)
    write_stable_artifact(
        root,
        artifact_id="screening_checkpoint/luanti/10/current/v1",
        artifact_kind="screening_checkpoint",
        semantic_version="1.0",
        created_at="2026-08-13T00:00:01Z",
        inputs=(),
        payload={
            "candidate": {"repository_key": "luanti", "change_number": 10},
            "state": "complete",
            "outcome": "source_rejected",
            "stage_artifact_id": "candidate_source_terminal/luanti/10/source/v1",
        },
        authorization=authorization,
    )

    with pytest.raises(RuntimeError, match="v2 screening checkpoint is missing"):
        module.main(
            [
                "--artifact-root",
                str(root),
                "--authorization-id",
                "local-aggregation/final-v2",
                "--execute",
            ]
        )

    with pytest.raises(RuntimeError, match="artifact directory is missing"):
        load_stable_artifact(
            root,
            artifact_id="screening_population/all/current/v2",
            artifact_kind="screening_population",
            semantic_version="2.0",
        )


def test_final_v2_aggregation_rejects_a_dangling_checkpoint_before_writing(
    tmp_path: Path,
) -> None:
    module = _load_module()
    root = tmp_path / "selection"
    authorization = _authorization()
    _write_v2_enumeration_population(root, authorization)
    write_stable_artifact(
        root,
        artifact_id="screening_checkpoint/luanti/10/current/v2",
        artifact_kind="screening_checkpoint",
        semantic_version="2.0",
        created_at="2026-08-13T00:00:01Z",
        inputs=(),
        payload={
            "candidate": {"repository_key": "luanti", "change_number": 10},
            "state": "complete",
            "outcome": "source_rejected",
            "stage_artifact_id": "candidate_source_terminal/luanti/10/source/v2",
        },
        authorization=authorization,
    )

    with pytest.raises(
        RuntimeError, match="v2 screening checkpoint binding is invalid"
    ):
        module.main(
            [
                "--artifact-root",
                str(root),
                "--authorization-id",
                "local-aggregation/final-v2",
                "--execute",
            ]
        )

    with pytest.raises(RuntimeError, match="artifact directory is missing"):
        load_stable_artifact(
            root,
            artifact_id="screening_population/all/current/v2",
            artifact_kind="screening_population",
            semantic_version="2.0",
        )


def test_final_v2_aggregation_refuses_missing_audit_before_writing(
    tmp_path: Path,
) -> None:
    module = _load_module()
    root = tmp_path / "selection"
    authorization = _authorization()
    _write_v2_enumeration_population(root, authorization, write_audits=False)
    _write_v2_screening_terminal(root, CandidateKey("luanti", 10), authorization)
    _write_v2_screening_terminal(root, CandidateKey("jupyterlab", 30), authorization)

    with pytest.raises(
        RuntimeError, match="v2 enumeration audit is missing or invalid"
    ):
        module.main(
            [
                "--artifact-root",
                str(root),
                "--authorization-id",
                "local-aggregation/final-v2",
                "--execute",
            ]
        )

    with pytest.raises(RuntimeError, match="artifact directory is missing"):
        load_stable_artifact(
            root,
            artifact_id="screening_population/all/current/v2",
            artifact_kind="screening_population",
            semantic_version="2.0",
        )


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("c1_v2_population_cli", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("v2 population aggregation CLI cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_v2_enumeration_population(
    root: Path,
    authorization: AuthorizationRecord,
    *,
    write_audits: bool = True,
) -> None:
    enumerations = (
        RepositoryEnumeration(
            repository_key="luanti",
            merged_change_numbers=(10,),
            authoritative_issue_link_missing=(),
            repair_not_in_pinned=(),
            infrastructure_blocked=(),
            candidates=(CandidateKey("luanti", 10),),
        ),
        RepositoryEnumeration(
            repository_key="blender",
            merged_change_numbers=(20,),
            authoritative_issue_link_missing=(20,),
            repair_not_in_pinned=(),
            infrastructure_blocked=(),
            candidates=(),
        ),
        RepositoryEnumeration(
            repository_key="jupyterlab",
            merged_change_numbers=(30,),
            authoritative_issue_link_missing=(),
            repair_not_in_pinned=(),
            infrastructure_blocked=(),
            candidates=(CandidateKey("jupyterlab", 30),),
        ),
        RepositoryEnumeration(
            repository_key="openra",
            merged_change_numbers=(40,),
            authoritative_issue_link_missing=(),
            repair_not_in_pinned=(40,),
            infrastructure_blocked=(),
            candidates=(),
        ),
        RepositoryEnumeration(
            repository_key="libreoffice",
            merged_change_numbers=(50,),
            authoritative_issue_link_missing=(),
            repair_not_in_pinned=(),
            infrastructure_blocked=(50,),
            candidates=(),
        ),
    )
    for enumeration in enumerations:
        write_repository_enumeration(
            root,
            enumeration,
            authorization=authorization,
            selection_epoch=SELECTION_EPOCH_V2,
        )
        if write_audits:
            write_enumeration_audit_population(
                root,
                enumeration,
                authorization=authorization,
                persistence_workers=1,
                selection_epoch=SELECTION_EPOCH_V2,
            )


def _write_v2_screening_terminal(
    root: Path,
    candidate: CandidateKey,
    authorization: AuthorizationRecord,
) -> None:
    result = run_screening_batch(
        root,
        repository_key=candidate.repository_key,
        source_capture=lambda received: _source_rejection(received, candidate),
        patch_capture=lambda _candidate, _source: (_ for _ in ()).throw(
            AssertionError("rejected source must not enter patch screening")
        ),
        authorization=authorization,
        selection_epoch=SELECTION_EPOCH_V2,
        source_workers=1,
        patch_workers=1,
        persistence_workers=1,
        reload_workers=1,
        queue_limit=1,
    )
    assert result.total_candidates == 1
    assert result.source_rejected == 1


def _source_rejection(
    received: CandidateKey,
    expected: CandidateKey,
) -> SourceDecision:
    assert received == expected
    return SourceDecision.rejected("authoritative_issue_link_missing")


def _authorization() -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id="local-aggregation/test",
        scope="candidate-screening",
        granted_at="2026-08-13T00:00:00Z",
        local_user_id=os.getuid(),
    )
