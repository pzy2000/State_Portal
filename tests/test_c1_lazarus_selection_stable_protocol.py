from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from warptest.c1_lazarus_selection.stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    load_stable_artifact,
    write_stable_artifact,
)
import pytest


def test_stable_artifact_round_trip_uses_semantic_identity_and_current_head(
    tmp_path: Path,
) -> None:
    root = tmp_path / "selection"
    authorization = AuthorizationRecord(
        authorization_id="local-screening/migration-1",
        scope="candidate-screening",
        granted_at="2026-08-13T00:00:00Z",
        local_user_id=os.getuid(),
    )
    dependency = ArtifactReference(
        artifact_id="repository_enumeration/blender/current/v1",
        artifact_kind="repository_enumeration",
        semantic_version="1.0",
    )

    written = write_stable_artifact(
        root,
        artifact_id="candidate_test_patch/blender/136987/screening/v1",
        artifact_kind="candidate_test_patch",
        semantic_version="1.0",
        created_at="2026-08-13T00:01:00Z",
        inputs=(dependency,),
        payload={
            "candidate": {"repository_key": "blender", "change_number": 136987},
            "status": "patch_rejected",
            "complete": True,
        },
        authorization=authorization,
    )
    loaded = load_stable_artifact(
        root,
        artifact_id="candidate_test_patch/blender/136987/screening/v1",
        artifact_kind="candidate_test_patch",
        semantic_version="1.0",
    )

    assert written == loaded.revision_path
    assert loaded.reference == ArtifactReference(
        artifact_id="candidate_test_patch/blender/136987/screening/v1",
        artifact_kind="candidate_test_patch",
        semantic_version="1.0",
    )
    assert loaded.payload["candidate"] == {
        "repository_key": "blender",
        "change_number": 136987,
    }
    assert loaded.inputs == (dependency,)
    assert loaded.authorization == authorization
    assert loaded.revision_path.name == "2026-08-13T00-01-00Z.json"

    raw = json.loads(loaded.revision_path.read_text(encoding="utf-8"))
    assert set(raw) == {
        "protocol",
        "artifact_id",
        "artifact_kind",
        "semantic_version",
        "created_at",
        "inputs",
        "payload",
        "authorization",
    }
    assert json.loads(loaded.head_path.read_text(encoding="utf-8")) == {
        "protocol": "c1-lazarus-stable-head-v1",
        "artifact_id": "candidate_test_patch/blender/136987/screening/v1",
        "artifact_kind": "candidate_test_patch",
        "semantic_version": "1.0",
        "created_at": "2026-08-13T00:01:00Z",
        "revision": "2026-08-13T00-01-00Z.json",
    }


def test_stable_artifact_resume_ignores_producer_source_changes(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "selection"
    authorization = AuthorizationRecord(
        authorization_id="local-screening/migration-1",
        scope="candidate-screening",
        granted_at="2026-08-13T00:00:00Z",
        local_user_id=os.getuid(),
    )
    write_stable_artifact(
        root,
        artifact_id="checkpoint/blender/136987/source/v1",
        artifact_kind="checkpoint",
        semantic_version="1.0",
        created_at="2026-08-13T00:01:00Z",
        inputs=(),
        payload={"state": "complete", "next_stage": "patch"},
        authorization=authorization,
    )

    monkeypatch.setattr(
        "warptest.c1_lazarus_selection.stable_protocol.__file__",
        "/different/producer/source.py",
    )
    loaded = load_stable_artifact(
        root,
        artifact_id="checkpoint/blender/136987/source/v1",
        artifact_kind="checkpoint",
        semantic_version="1.0",
    )

    assert loaded.payload == {"state": "complete", "next_stage": "patch"}


def test_stable_artifact_rejects_revoked_authorization_and_symlink_head(
    tmp_path: Path,
) -> None:
    root = tmp_path / "selection"
    authorization = AuthorizationRecord(
        authorization_id="local-screening/migration-1",
        scope="candidate-screening",
        granted_at="2026-08-13T00:00:00Z",
        local_user_id=os.getuid(),
    )
    write_stable_artifact(
        root,
        artifact_id="checkpoint/blender/136987/source/v1",
        artifact_kind="checkpoint",
        semantic_version="1.0",
        created_at="2026-08-13T00:01:00Z",
        inputs=(),
        payload={"state": "complete"},
        authorization=authorization,
    )
    loaded = load_stable_artifact(
        root,
        artifact_id="checkpoint/blender/136987/source/v1",
        artifact_kind="checkpoint",
        semantic_version="1.0",
    )
    loaded.head_path.unlink()
    loaded.head_path.symlink_to(loaded.revision_path.name)

    with pytest.raises(RuntimeError, match="unsafe"):
        load_stable_artifact(
            root,
            artifact_id="checkpoint/blender/136987/source/v1",
            artifact_kind="checkpoint",
            semantic_version="1.0",
        )
    with pytest.raises(PermissionError, match="not active"):
        write_stable_artifact(
            root,
            artifact_id="checkpoint/blender/136987/patch/v1",
            artifact_kind="checkpoint",
            semantic_version="1.0",
            created_at="2026-08-13T00:02:00Z",
            inputs=(),
            payload={"state": "complete"},
            authorization=replace(authorization, revoked=True),
        )


def test_stable_artifact_accepts_microsecond_revisions_without_collision(
    tmp_path: Path,
) -> None:
    root = tmp_path / "selection"
    authorization = AuthorizationRecord(
        authorization_id="local-screening/run-2",
        scope="candidate-screening",
        granted_at="2026-08-13T00:00:00Z",
        local_user_id=os.getuid(),
    )

    first = write_stable_artifact(
        root,
        artifact_id="checkpoint/blender/136987/current/v1",
        artifact_kind="checkpoint",
        semantic_version="1.0",
        created_at="2026-08-13T00:01:00.000001Z",
        inputs=(),
        payload={"state": "source_complete"},
        authorization=authorization,
    )
    second = write_stable_artifact(
        root,
        artifact_id="checkpoint/blender/136987/current/v1",
        artifact_kind="checkpoint",
        semantic_version="1.0",
        created_at="2026-08-13T00:01:00.000002Z",
        inputs=(),
        payload={"state": "patch_complete"},
        authorization=authorization,
    )

    assert first != second
    assert load_stable_artifact(
        root,
        artifact_id="checkpoint/blender/136987/current/v1",
        artifact_kind="checkpoint",
        semantic_version="1.0",
    ).payload == {"state": "patch_complete"}


def test_concurrent_stable_writers_leave_the_latest_current_head(
    tmp_path: Path,
) -> None:
    root = tmp_path / "selection"
    authorization = AuthorizationRecord(
        authorization_id="local-screening/run-3",
        scope="candidate-screening",
        granted_at="2026-08-13T00:00:00Z",
        local_user_id=os.getuid(),
    )

    def write(sequence: int) -> None:
        write_stable_artifact(
            root,
            artifact_id="checkpoint/blender/136987/current/v1",
            artifact_kind="checkpoint",
            semantic_version="1.0",
            created_at=f"2026-08-13T00:01:00.{sequence:06d}Z",
            inputs=(),
            payload={"sequence": sequence},
            authorization=authorization,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        tuple(pool.map(write, range(1, 33)))

    loaded = load_stable_artifact(
        root,
        artifact_id="checkpoint/blender/136987/current/v1",
        artifact_kind="checkpoint",
        semantic_version="1.0",
    )
    assert loaded.created_at == "2026-08-13T00:01:00.000032Z"
    assert loaded.payload == {"sequence": 32}
