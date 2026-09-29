from __future__ import annotations

import os
from pathlib import Path

import pytest

from warptest.c1_lazarus_selection import (
    jupyterlab_historical_runtime_pool_blocker as target,
)
from warptest.c1_lazarus_selection.stable_protocol import (
    AuthorizationRecord,
    write_stable_artifact,
)


def _authorization(scope: str = "infrastructure-audit") -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id=f"test/jupyterlab-historical-runtime/{scope}",
        scope=scope,
        granted_at="2026-08-15T03:00:00Z",
        local_user_id=os.getuid(),
    )


def _seed(root: Path, artifact_id: str, outcome: str) -> None:
    write_stable_artifact(
        root,
        artifact_id=artifact_id,
        artifact_kind="dependency_preparation",
        semantic_version="1.0",
        created_at="2026-08-15T02:00:00Z",
        inputs=(),
        payload={"outcome": outcome, "complete": True, "dependency_id": artifact_id},
        authorization=_authorization(),
    )


def test_blocker_records_every_registered_preparation_as_not_ready(
    tmp_path: Path,
) -> None:
    _seed(tmp_path, "dependency_preparation/jupyterlab-node5-karma1/current/v1", "pending")
    _seed(
        tmp_path,
        "dependency_preparation/jupyterlab-3703-node5-firefox57/current/v1",
        "infrastructure_blocked",
    )

    artifact = target.write_historical_runtime_pool_blocker(
        tmp_path, authorization=_authorization()
    )

    assert artifact.payload["outcome"] == "infrastructure_blocked"
    assert artifact.payload["terminal"] is False
    assert artifact.payload["resumable"] is True
    assert artifact.payload["ready_preparations"] == []
    assert artifact.payload["candidate_execution_performed"] is False
    assert [item["change_number"] for item in artifact.payload["candidates"]] == list(
        target.CANDIDATES
    )
    assert len(artifact.payload["registered_preparations"]) == 2
    assert len(artifact.inputs) == 2


def test_a_ready_runtime_forbids_writing_the_blocker(tmp_path: Path) -> None:
    _seed(tmp_path, "dependency_preparation/jupyterlab-node5-karma1/current/v1", "ready")

    with pytest.raises(RuntimeError, match="ready historical runtime exists"):
        target.write_historical_runtime_pool_blocker(
            tmp_path, authorization=_authorization()
        )


def test_missing_preparations_and_wrong_scope_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="no JupyterLab historical runtime"):
        target.write_historical_runtime_pool_blocker(
            tmp_path, authorization=_authorization()
        )
    _seed(tmp_path, "dependency_preparation/jupyterlab-node5-karma1/current/v1", "pending")
    with pytest.raises(PermissionError):
        target.write_historical_runtime_pool_blocker(
            tmp_path, authorization=_authorization("preannotation-execution")
        )
