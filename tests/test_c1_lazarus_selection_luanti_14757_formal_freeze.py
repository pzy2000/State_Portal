from __future__ import annotations

import os
from pathlib import Path

import pytest

from warptest.c1_lazarus_selection.luanti_14757_formal_freeze import (
    _ARTIFACT,
    _write_once,
)
from warptest.c1_lazarus_selection.stable_protocol import (
    AuthorizationRecord,
    load_stable_artifact,
)


def _authorization() -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id="local-test/luanti-14757-freeze",
        scope="screening",
        granted_at="2026-08-14T10:00:00Z",
        local_user_id=os.getuid(),
    )


def test_formal_freeze_receipt_write_is_idempotent_and_stable_id(
    tmp_path: Path,
) -> None:
    payload = {
        "candidate": {"repository_key": "luanti", "change_number": 14757},
        "freeze_state": "admitted",
        "formal_trials_authorized": False,
        "complete": True,
    }
    _write_once(tmp_path, (), payload, _authorization())
    _write_once(tmp_path, (), payload, _authorization())
    artifact = load_stable_artifact(
        tmp_path,
        artifact_id=_ARTIFACT.artifact_id,
        artifact_kind=_ARTIFACT.artifact_kind,
        semantic_version=_ARTIFACT.semantic_version,
    )
    assert artifact.payload == payload
    assert not any(
        token in repr(artifact.payload).lower()
        for token in ("sha256", "sha-256", "hmac", "digest")
    )
    with pytest.raises(RuntimeError, match="current head conflicts"):
        _write_once(
            tmp_path,
            (),
            {**payload, "freeze_state": "changed"},
            _authorization(),
        )
