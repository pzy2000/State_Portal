from __future__ import annotations

import os
from pathlib import Path

import pytest

from warptest.c1_lazarus_selection.readiness import (
    REQUIRED_READINESS_GATES,
    load_implementation_readiness,
    write_implementation_readiness,
    write_readiness_gate,
)
from warptest.c1_lazarus_selection.stable_protocol import (
    AuthorizationRecord,
    load_stable_artifact,
    write_stable_artifact,
)


def _authorization() -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id="local-readiness/run-1",
        scope="implementation-readiness",
        granted_at="2026-08-13T00:00:00Z",
        local_user_id=os.getuid(),
    )


def _scope(root: Path, name: str):
    write_stable_artifact(
        root,
        artifact_id=f"formal_schedule/{name}/v1",
        artifact_kind="formal_schedule",
        semantic_version="1.0",
        created_at="2026-08-13T00:01:00Z",
        inputs=(),
        payload={"name": name, "complete": True},
        authorization=_authorization(),
    )
    return load_stable_artifact(
        root,
        artifact_id=f"formal_schedule/{name}/v1",
        artifact_kind="formal_schedule",
        semantic_version="1.0",
    )


def test_readiness_gates_bind_exact_scope_not_backdated_timestamps(
    tmp_path: Path,
) -> None:
    root = tmp_path / "selection"
    first = _scope(root, "scope-a")
    gates = tuple(
        write_readiness_gate(
            root,
            scope=first,
            gate_name=name,
            evidence_path=f"reports/readiness/{name}.json",
            authorization=_authorization(),
        )
        for name in REQUIRED_READINESS_GATES
    )
    ready = write_implementation_readiness(
        root,
        scope=first,
        gate_artifact_ids=tuple(gate.reference.artifact_id for gate in gates),
        authorization=_authorization(),
    )

    assert (
        load_implementation_readiness(
            root,
            scope=first,
            artifact_id=ready.reference.artifact_id,
        ).payload["complete"]
        is True
    )

    second = _scope(root, "scope-b")
    with pytest.raises(RuntimeError, match="scope"):
        write_implementation_readiness(
            root,
            scope=second,
            gate_artifact_ids=tuple(gate.reference.artifact_id for gate in gates),
            authorization=_authorization(),
        )
