from __future__ import annotations

import os
from pathlib import Path

from warptest.c1_lazarus_selection.audit_labels import write_old24_audit_labels
from warptest.c1_lazarus_selection.enumeration import (
    RepositoryEnumeration,
    write_enumeration_audit_population,
    write_repository_enumeration,
)
from warptest.c1_lazarus_selection.models import CandidateKey
from warptest.c1_lazarus_selection.stable_protocol import (
    AuthorizationRecord,
    load_stable_artifact,
    write_stable_artifact,
)


def test_old24_are_audit_labels_with_typed_unresolved_mapping(tmp_path: Path) -> None:
    root = tmp_path / "selection"
    authorization = AuthorizationRecord(
        authorization_id="local-audit/old24-run-1",
        scope="candidate-screening",
        granted_at="2026-08-13T00:00:00Z",
        local_user_id=os.getuid(),
    )
    enumeration = RepositoryEnumeration(
        repository_key="luanti",
        merged_change_numbers=(10, 11),
        authoritative_issue_link_missing=(10,),
        repair_not_in_pinned=(),
        candidates=(CandidateKey("luanti", 11),),
    )
    write_repository_enumeration(
        root,
        enumeration,
        authorization=authorization,
    )
    write_enumeration_audit_population(
        root,
        enumeration,
        authorization=authorization,
    )
    source = tmp_path / "old24.yaml"
    source.write_text(
        """population:
  task_count: 3
candidates:
  - task_id: old-luanti-10
    benchmark: luanti
    pr_number: 10
  - task_id: old-luanti-11
    benchmark: luanti
    pr_number: 11
  - task_id: old-jupyter-unknown
    benchmark: jupyterlab
    pr_number: null
"""
    )

    result = write_old24_audit_labels(
        root,
        source_path=source,
        authorization=authorization,
        expected_count=3,
    )

    assert result.total == 3
    assert result.mapped == 2
    assert result.unresolved == 1
    missing = load_stable_artifact(
        root,
        artifact_id="old24_audit/old-luanti-10/current/v1",
        artifact_kind="old24_audit",
        semantic_version="1.0",
    )
    assert missing.payload["mapping_state"] == "mapped"
    assert missing.payload["enumeration_outcome"] == (
        "authoritative_issue_link_missing"
    )
    unresolved = load_stable_artifact(
        root,
        artifact_id="old24_audit/old-jupyter-unknown/current/v1",
        artifact_kind="old24_audit",
        semantic_version="1.0",
    )
    assert unresolved.payload == {
        "task_label": "old-jupyter-unknown",
        "repository_key": "jupyterlab",
        "change_number": None,
        "mapping_state": "unresolved",
        "reason": "change_number_missing",
        "inherited_conclusions": False,
    }


def test_old24_audit_uses_same_preannotation_terminal_as_every_candidate(
    tmp_path: Path,
) -> None:
    candidate = CandidateKey("jupyterlab", 30)
    write_repository_enumeration(
        tmp_path,
        RepositoryEnumeration(
            repository_key="jupyterlab",
            merged_change_numbers=(30,),
            authoritative_issue_link_missing=(),
            repair_not_in_pinned=(),
            candidates=(candidate,),
        ),
        authorization=_authorization_for_test(),
    )
    write_stable_artifact(
        tmp_path,
        artifact_id="screening_checkpoint/jupyterlab/30/current/v1",
        artifact_kind="screening_checkpoint",
        semantic_version="1.0",
        created_at="2026-08-13T00:00:01Z",
        inputs=(),
        payload={
            "candidate": candidate.to_value(),
            "state": "complete",
            "outcome": "patch_eligible",
        },
        authorization=_authorization_for_test(),
    )
    write_stable_artifact(
        tmp_path,
        artifact_id="preannotation_checkpoint/jupyterlab/30/current/v1",
        artifact_kind="preannotation_checkpoint",
        semantic_version="1.0",
        created_at="2026-08-13T00:00:02Z",
        inputs=(),
        payload={
            "candidate": candidate.to_value(),
            "state": "terminal",
            "outcome": "quality_rejected",
        },
        authorization=_authorization_for_test(),
    )
    source = tmp_path / "old24-terminal.yaml"
    source.write_text(
        """candidates:
  - task_id: old-jupyter-30
    benchmark: jupyterlab
    pr_number: 30
"""
    )

    result = write_old24_audit_labels(
        tmp_path,
        source_path=source,
        authorization=_authorization_for_test(),
        expected_count=1,
    )

    assert result.terminal == 1
    assert result.eligible == 0
    artifact = load_stable_artifact(
        tmp_path,
        artifact_id="old24_audit/old-jupyter-30/current/v1",
        artifact_kind="old24_audit",
        semantic_version="1.0",
    )
    assert artifact.payload["screening_outcome"] == "quality_rejected"


def _authorization_for_test() -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id="local-audit/old24-terminal",
        scope="candidate-screening",
        granted_at="2026-08-13T00:00:00Z",
        local_user_id=os.getuid(),
    )
