from __future__ import annotations

import os
from pathlib import Path

import pytest

from warptest.c1_lazarus_selection.annotation import (
    AnnotationCapture,
    AnnotationRequest,
    run_annotation_panel,
    write_annotation_authorization,
    write_annotation_request_matrix,
)
from warptest.c1_lazarus_selection.models import CandidateKey
from warptest.c1_lazarus_selection.readiness import (
    REQUIRED_READINESS_GATES,
    write_implementation_readiness,
    write_readiness_gate,
)
from warptest.c1_lazarus_selection.stable_protocol import (
    AuthorizationRecord,
    load_stable_artifact,
)


def _authorization() -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id="local-annotation/run-1",
        scope="paid-annotation",
        granted_at="2026-08-13T00:00:00Z",
        local_user_id=os.getuid(),
    )


def _requests() -> tuple[AnnotationRequest, ...]:
    return tuple(
        AnnotationRequest(
            candidate=CandidateKey("jupyterlab", number),
            pass_kind=pass_kind,
            provider=provider,
            raw_request_path=(
                f"raw/annotation/request/jupyterlab/{number}/"
                f"{pass_kind}/{provider}.json"
            ),
        )
        for number in (10, 11)
        for pass_kind in ("problem", "test", "construction")
        for provider in ("provider-a", "provider-b", "provider-c")
    )


def _ready(root: Path, scope):
    gates = tuple(
        write_readiness_gate(
            root,
            scope=scope,
            gate_name=name,
            evidence_path=f"reports/readiness/{name}.json",
            authorization=_authorization(),
        )
        for name in REQUIRED_READINESS_GATES
    )
    return write_implementation_readiness(
        root,
        scope=scope,
        gate_artifact_ids=tuple(gate.reference.artifact_id for gate in gates),
        authorization=_authorization(),
    )


def test_annotation_dispatch_requires_exact_user_authorization(tmp_path: Path) -> None:
    root = tmp_path / "selection"
    matrix = write_annotation_request_matrix(
        root,
        requests=_requests(),
        authorization=_authorization(),
    )
    calls = 0

    def execute(_request):
        nonlocal calls
        calls += 1
        raise AssertionError("provider called without authorization")

    with pytest.raises(RuntimeError, match="authorization"):
        run_annotation_panel(
            root,
            request_matrix_artifact_id=matrix.reference.artifact_id,
            execute=execute,
            authorization=_authorization(),
        )
    assert calls == 0


def test_annotation_failure_preserves_attempt_and_blocks_seal(tmp_path: Path) -> None:
    root = tmp_path / "selection"
    requests = _requests()
    matrix = write_annotation_request_matrix(
        root,
        requests=requests,
        authorization=_authorization(),
    )
    write_annotation_authorization(
        root,
        request_matrix_artifact_id=matrix.reference.artifact_id,
        readiness_artifact_id=_ready(root, matrix).reference.artifact_id,
        confirmation_reference="user-confirmation/annotation-test-1",
        authorization=_authorization(),
    )
    calls = 0

    def execute(request: AnnotationRequest) -> AnnotationCapture:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise TimeoutError("provider timeout")
        return AnnotationCapture(
            state="succeeded",
            raw_response_path=(
                f"raw/annotation/response/{request.provider}/{calls}.json"
            ),
            request_id=f"request-{calls}",
            input_tokens=10,
            output_tokens=5,
            elapsed_ms=100.0,
            failure_type=None,
        )

    with pytest.raises(RuntimeError, match="did not complete"):
        run_annotation_panel(
            root,
            request_matrix_artifact_id=matrix.reference.artifact_id,
            execute=execute,
            authorization=_authorization(),
        )

    first = load_stable_artifact(
        root,
        artifact_id="annotation_attempt/1/current/v1",
        artifact_kind="annotation_attempt",
        semantic_version="1.0",
    )
    second = load_stable_artifact(
        root,
        artifact_id="annotation_attempt/2/current/v1",
        artifact_kind="annotation_attempt",
        semantic_version="1.0",
    )
    assert first.payload["state"] == "succeeded"
    assert second.payload["state"] == "timeout"
    assert second.payload["request_id"] is None
    with pytest.raises(RuntimeError, match="missing"):
        load_stable_artifact(
            root,
            artifact_id="annotation_attempt_registry/current/v1",
            artifact_kind="annotation_attempt_registry",
            semantic_version="1.0",
        )


def test_annotation_success_seals_exact_ordered_registry(tmp_path: Path) -> None:
    root = tmp_path / "selection"
    requests = _requests()
    matrix = write_annotation_request_matrix(
        root,
        requests=requests,
        authorization=_authorization(),
    )
    write_annotation_authorization(
        root,
        request_matrix_artifact_id=matrix.reference.artifact_id,
        readiness_artifact_id=_ready(root, matrix).reference.artifact_id,
        confirmation_reference="user-confirmation/annotation-test-2",
        authorization=_authorization(),
    )

    def execute(request: AnnotationRequest) -> AnnotationCapture:
        index = requests.index(request) + 1
        return AnnotationCapture(
            state="succeeded",
            raw_response_path=f"raw/annotation/response/{index}.json",
            request_id=f"request-{index}",
            input_tokens=10,
            output_tokens=5,
            elapsed_ms=100.0,
            failure_type=None,
        )

    registry = run_annotation_panel(
        root,
        request_matrix_artifact_id=matrix.reference.artifact_id,
        execute=execute,
        authorization=_authorization(),
    )

    assert registry.payload["state"] == "sealed"
    assert registry.payload["attempt_count"] == 18
    assert len(registry.inputs) == 20
