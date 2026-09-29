"""Stable paid-annotation request and append-only attempt protocol."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Literal

from .models import CandidateKey
from .resource_guard import require_write_capacity
from .readiness import load_implementation_readiness
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_PASSES = ("problem", "test", "construction")


@dataclass(frozen=True, order=True)
class AnnotationRequest:
    candidate: CandidateKey
    pass_kind: Literal["problem", "test", "construction"]
    provider: str
    raw_request_path: str

    def __post_init__(self) -> None:
        if type(self.candidate) is not CandidateKey:
            raise ValueError("annotation request candidate is invalid")
        if self.pass_kind not in _PASSES:
            raise ValueError("annotation request pass is invalid")
        if type(self.provider) is not str or not self.provider:
            raise ValueError("annotation request provider is invalid")
        if type(self.raw_request_path) is not str or not self.raw_request_path:
            raise ValueError("annotation request raw path is invalid")


@dataclass(frozen=True)
class AnnotationCapture:
    state: Literal["succeeded", "failed", "timeout"]
    raw_response_path: str | None
    request_id: str | None
    input_tokens: int | None
    output_tokens: int | None
    elapsed_ms: float | None
    failure_type: str | None

    def __post_init__(self) -> None:
        if self.state == "succeeded":
            if (
                type(self.raw_response_path) is not str
                or not self.raw_response_path
                or type(self.request_id) is not str
                or not self.request_id
                or type(self.input_tokens) is not int
                or self.input_tokens < 0
                or type(self.output_tokens) is not int
                or self.output_tokens < 0
                or type(self.elapsed_ms) is not float
                or self.elapsed_ms < 0
                or self.failure_type is not None
            ):
                raise ValueError("successful annotation capture is invalid")
        elif self.state in {"failed", "timeout"}:
            if type(self.failure_type) is not str or not self.failure_type:
                raise ValueError("failed annotation capture lacks failure type")
        else:
            raise ValueError("annotation capture state is invalid")


def write_annotation_request_matrix(
    artifact_root: str | Path,
    *,
    requests: tuple[AnnotationRequest, ...],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Write one exact ordered three-pass, three-provider matrix."""

    if type(requests) is not tuple or any(
        type(request) is not AnnotationRequest for request in requests
    ):
        raise ValueError("annotation requests are invalid")
    candidates = tuple(sorted({request.candidate for request in requests}))
    providers = tuple(sorted({request.provider for request in requests}))
    if len(providers) != 3:
        raise ValueError("annotation matrix must use exactly three providers")
    expected = tuple(
        AnnotationRequest(candidate, pass_kind, provider, request.raw_request_path)
        for candidate in candidates
        for pass_kind in _PASSES
        for provider in providers
        for request in requests
        if request.candidate == candidate
        and request.pass_kind == pass_kind
        and request.provider == provider
    )
    if requests != expected or len(requests) != len(candidates) * 9:
        raise ValueError("annotation matrix order or coverage is invalid")
    payload = {
        "requests": [_request_value(request) for request in requests],
        "candidate_count": len(candidates),
        "request_count": len(requests),
        "providers": list(providers),
        "passes": list(_PASSES),
        "paid_authorized": False,
    }
    write_stable_artifact(
        artifact_root,
        artifact_id="annotation_request_matrix/current/v1",
        artifact_kind="annotation_request_matrix",
        semantic_version="1.0",
        created_at=_now(),
        inputs=(),
        payload=payload,
        authorization=authorization,
    )
    return load_stable_artifact(
        artifact_root,
        artifact_id="annotation_request_matrix/current/v1",
        artifact_kind="annotation_request_matrix",
        semantic_version="1.0",
    )


def write_annotation_authorization(
    artifact_root: str | Path,
    *,
    request_matrix_artifact_id: str,
    readiness_artifact_id: str,
    confirmation_reference: str,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Bind explicit user confirmation to one exact matrix head."""

    matrix = _load_matrix(artifact_root, request_matrix_artifact_id)
    readiness = load_implementation_readiness(
        artifact_root,
        scope=matrix,
        artifact_id=readiness_artifact_id,
    )
    if type(confirmation_reference) is not str or not confirmation_reference:
        raise ValueError("annotation confirmation reference is invalid")
    write_stable_artifact(
        artifact_root,
        artifact_id="annotation_authorization/current/v1",
        artifact_kind="annotation_authorization",
        semantic_version="1.0",
        created_at=_now(),
        inputs=(matrix.reference, readiness.reference),
        payload={
            "request_matrix_artifact_id": matrix.reference.artifact_id,
            "request_matrix_created_at": matrix.created_at,
            "readiness_artifact_id": readiness.reference.artifact_id,
            "confirmation_reference": confirmation_reference,
            "action": "paid_annotation",
            "active": True,
        },
        authorization=authorization,
    )
    return load_stable_artifact(
        artifact_root,
        artifact_id="annotation_authorization/current/v1",
        artifact_kind="annotation_authorization",
        semantic_version="1.0",
    )


def run_annotation_panel(
    artifact_root: str | Path,
    *,
    request_matrix_artifact_id: str,
    execute: Callable[[AnnotationRequest], AnnotationCapture],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Dispatch sequentially and persist every terminal attempt immediately."""

    root = Path(artifact_root)
    matrix = _load_matrix(root, request_matrix_artifact_id)
    grant = _load_authorization(root, matrix)
    requests = tuple(_request_from_value(value) for value in matrix.payload["requests"])
    attempts: list[ArtifactReference] = []
    request_ids: set[str] = set()
    all_succeeded = True
    for index, request in enumerate(requests, start=1):
        reservation_id = f"annotation_reservation/{index}/current/v1"
        require_write_capacity(root)
        write_stable_artifact(
            root,
            artifact_id=reservation_id,
            artifact_kind="annotation_reservation",
            semantic_version="1.0",
            created_at=_now(),
            inputs=(matrix.reference, grant.reference),
            payload={
                "sequence": index,
                "request": _request_value(request),
                "state": "reserved",
            },
            authorization=authorization,
        )
        reservation = load_stable_artifact(
            root,
            artifact_id=reservation_id,
            artifact_kind="annotation_reservation",
            semantic_version="1.0",
        )
        try:
            capture = execute(request)
            if type(capture) is not AnnotationCapture:
                raise RuntimeError("annotation transport returned an untyped capture")
        except TimeoutError as error:
            capture = AnnotationCapture(
                state="timeout",
                raw_response_path=None,
                request_id=None,
                input_tokens=None,
                output_tokens=None,
                elapsed_ms=None,
                failure_type=type(error).__name__,
            )
        except Exception as error:
            capture = AnnotationCapture(
                state="failed",
                raw_response_path=None,
                request_id=None,
                input_tokens=None,
                output_tokens=None,
                elapsed_ms=None,
                failure_type=type(error).__name__,
            )
        if capture.request_id is not None:
            if capture.request_id in request_ids:
                raise RuntimeError("annotation provider request ID is duplicated")
            request_ids.add(capture.request_id)
        attempt_id = f"annotation_attempt/{index}/current/v1"
        write_stable_artifact(
            root,
            artifact_id=attempt_id,
            artifact_kind="annotation_attempt",
            semantic_version="1.0",
            created_at=_now(),
            inputs=(reservation.reference,),
            payload={
                "sequence": index,
                "request": _request_value(request),
                "state": capture.state,
                "raw_response_path": capture.raw_response_path,
                "request_id": capture.request_id,
                "input_tokens": capture.input_tokens,
                "output_tokens": capture.output_tokens,
                "elapsed_ms": capture.elapsed_ms,
                "failure_type": capture.failure_type,
            },
            authorization=authorization,
        )
        attempt = load_stable_artifact(
            root,
            artifact_id=attempt_id,
            artifact_kind="annotation_attempt",
            semantic_version="1.0",
        )
        attempts.append(attempt.reference)
        if capture.state != "succeeded":
            all_succeeded = False
            break
    if not all_succeeded or len(attempts) != len(requests):
        raise RuntimeError("annotation panel did not complete; attempts were preserved")
    registry_inputs = (matrix.reference, grant.reference, *attempts)
    write_stable_artifact(
        root,
        artifact_id="annotation_attempt_registry/current/v1",
        artifact_kind="annotation_attempt_registry",
        semantic_version="1.0",
        created_at=_now(),
        inputs=registry_inputs,
        payload={
            "state": "sealed",
            "request_count": len(requests),
            "attempt_count": len(attempts),
            "attempt_artifact_ids": [value.artifact_id for value in attempts],
        },
        authorization=authorization,
    )
    return load_stable_artifact(
        root,
        artifact_id="annotation_attempt_registry/current/v1",
        artifact_kind="annotation_attempt_registry",
        semantic_version="1.0",
    )


def _load_matrix(root: str | Path, artifact_id: str) -> StableArtifact:
    matrix = load_stable_artifact(
        root,
        artifact_id=artifact_id,
        artifact_kind="annotation_request_matrix",
        semantic_version="1.0",
    )
    payload = matrix.payload
    if (
        payload.get("paid_authorized") is not False
        or type(payload.get("requests")) is not list
        or payload.get("request_count") != len(payload["requests"])
    ):
        raise RuntimeError("annotation request matrix is invalid")
    return matrix


def _load_authorization(root: Path, matrix: StableArtifact) -> StableArtifact:
    try:
        grant = load_stable_artifact(
            root,
            artifact_id="annotation_authorization/current/v1",
            artifact_kind="annotation_authorization",
            semantic_version="1.0",
        )
    except RuntimeError as error:
        raise RuntimeError("annotation execution authorization is missing") from error
    payload = grant.payload
    if (
        len(grant.inputs) != 2
        or grant.inputs[0] != matrix.reference
        or payload.get("request_matrix_artifact_id") != matrix.reference.artifact_id
        or payload.get("request_matrix_created_at") != matrix.created_at
        or payload.get("action") != "paid_annotation"
        or payload.get("active") is not True
        or type(payload.get("confirmation_reference")) is not str
    ):
        raise RuntimeError("annotation execution authorization is invalid")
    readiness = load_implementation_readiness(
        root,
        scope=matrix,
        artifact_id=payload.get("readiness_artifact_id"),
    )
    if grant.inputs[1] != readiness.reference:
        raise RuntimeError("annotation execution readiness is invalid")
    return grant


def _request_value(request: AnnotationRequest) -> dict[str, object]:
    return {
        "candidate": request.candidate.to_value(),
        "pass_kind": request.pass_kind,
        "provider": request.provider,
        "raw_request_path": request.raw_request_path,
    }


def _request_from_value(value: object) -> AnnotationRequest:
    if type(value) is not dict or set(value) != {
        "candidate",
        "pass_kind",
        "provider",
        "raw_request_path",
    }:
        raise RuntimeError("annotation request value is invalid")
    return AnnotationRequest(
        candidate=CandidateKey.from_value(value["candidate"]),
        pass_kind=value["pass_kind"],
        provider=value["provider"],
        raw_request_path=value["raw_request_path"],
    )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = [
    "AnnotationCapture",
    "AnnotationRequest",
    "run_annotation_panel",
    "write_annotation_authorization",
    "write_annotation_request_matrix",
]
