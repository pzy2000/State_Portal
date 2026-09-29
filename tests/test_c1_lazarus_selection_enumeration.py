from __future__ import annotations

import os
from pathlib import Path

import pytest

from warptest.c1_lazarus_selection.enumeration import (
    RepositoryEnumeration,
    load_repository_enumeration,
    write_enumeration_audit_population,
    write_repository_enumeration,
)
from warptest.c1_lazarus_selection.epoch import SELECTION_EPOCH_V2
from warptest.c1_lazarus_selection.models import CandidateKey
from warptest.c1_lazarus_selection.stable_protocol import (
    AuthorizationRecord,
    load_stable_artifact,
)


def test_enumeration_writes_exact_disjoint_population_and_typed_terminals(
    tmp_path: Path,
) -> None:
    root = tmp_path / "selection"
    authorization = AuthorizationRecord(
        authorization_id="local-enumeration/run-1",
        scope="repository-enumeration",
        granted_at="2026-08-13T00:00:00Z",
        local_user_id=os.getuid(),
    )
    enumeration = RepositoryEnumeration(
        repository_key="luanti",
        merged_change_numbers=(1, 2, 3, 4, 5),
        authoritative_issue_link_missing=(1, 2),
        repair_not_in_pinned=(3,),
        candidates=(CandidateKey("luanti", 4), CandidateKey("luanti", 5)),
    )

    write_repository_enumeration(root, enumeration, authorization=authorization)
    loaded = load_repository_enumeration(root, repository_key="luanti")
    audit = write_enumeration_audit_population(
        root, loaded, authorization=authorization, persistence_workers=2
    )

    assert loaded == enumeration
    assert audit.merged_count == 5
    assert audit.audit_count == 5
    assert audit.terminal_count == 3
    assert audit.deep_screen_count == 2
    missing = load_stable_artifact(
        root,
        artifact_id="enumeration_terminal/luanti/1/issue-link/v1",
        artifact_kind="enumeration_terminal",
        semantic_version="1.0",
    )
    not_pinned = load_stable_artifact(
        root,
        artifact_id="enumeration_terminal/luanti/3/not-in-pinned/v1",
        artifact_kind="enumeration_terminal",
        semantic_version="1.0",
    )
    assert missing.payload["reason"] == "authoritative_issue_link_missing"
    assert not_pinned.payload["reason"] == "repair_not_in_pinned"


def test_enumeration_v2_writer_and_audits_are_epoch_isolated(tmp_path: Path) -> None:
    root = tmp_path / "selection"
    authorization = AuthorizationRecord(
        authorization_id="local-enumeration/run-v2",
        scope="repository-enumeration",
        granted_at="2026-08-13T00:00:00Z",
        local_user_id=os.getuid(),
    )
    enumeration = RepositoryEnumeration(
        repository_key="luanti",
        merged_change_numbers=(1, 2, 3),
        authoritative_issue_link_missing=(1,),
        repair_not_in_pinned=(),
        candidates=(CandidateKey("luanti", 2),),
        infrastructure_blocked=(3,),
    )

    write_repository_enumeration(
        root,
        enumeration,
        authorization=authorization,
        selection_epoch=SELECTION_EPOCH_V2,
    )
    loaded = load_repository_enumeration(
        root,
        repository_key="luanti",
        selection_epoch=SELECTION_EPOCH_V2,
    )
    audit = write_enumeration_audit_population(
        root,
        loaded,
        authorization=authorization,
        persistence_workers=1,
        selection_epoch=SELECTION_EPOCH_V2,
    )

    assert loaded == enumeration
    assert audit.terminal_count == 2
    terminal = load_stable_artifact(
        root,
        artifact_id="enumeration_terminal/luanti/1/issue-link/v2",
        artifact_kind="enumeration_terminal",
        semantic_version="2.0",
    )
    assert terminal.inputs == (SELECTION_EPOCH_V2.enumeration_reference("luanti"),)
    infrastructure = load_stable_artifact(
        root,
        artifact_id="enumeration_terminal/luanti/3/infrastructure-blocked/v2",
        artifact_kind="enumeration_terminal",
        semantic_version="2.0",
    )
    assert infrastructure.payload["reason"] == "infrastructure_blocked"
    with pytest.raises(RuntimeError, match="artifact directory is missing"):
        load_repository_enumeration(root, repository_key="luanti")
