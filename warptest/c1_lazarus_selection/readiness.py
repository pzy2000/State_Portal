"""Scope-bound implementation readiness gates."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from .stable_protocol import (
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


REQUIRED_READINESS_GATES = (
    "targeted-tests",
    "full-selection-tests",
    "lint",
    "format",
    "compile",
    "workspace-diff",
    "repository-state",
    "filesystem-capacity",
    "runtime",
    "provider-routing",
    "provider-fake-exercise",
    "secret-scan",
    "schema-review",
    "raw-capture-review",
    "cache-review",
    "authorization-review",
    "recovery-review",
    "standards-review",
    "spec-review",
    "no-critical-findings",
)


def write_readiness_gate(
    artifact_root: str | Path,
    *,
    scope: StableArtifact,
    gate_name: str,
    evidence_path: str,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Write one passed gate bound directly to one current scope revision."""

    if gate_name not in REQUIRED_READINESS_GATES:
        raise ValueError("readiness gate name is invalid")
    if type(scope) is not StableArtifact:
        raise ValueError("readiness scope is invalid")
    if type(evidence_path) is not str or not evidence_path:
        raise ValueError("readiness gate evidence path is invalid")
    artifact_id = (
        f"readiness_gate/{scope.reference.artifact_kind}/{gate_name}/current/v1"
    )
    write_stable_artifact(
        artifact_root,
        artifact_id=artifact_id,
        artifact_kind="readiness_gate",
        semantic_version="1.0",
        created_at=_now(),
        inputs=(scope.reference,),
        payload={
            "scope_artifact_id": scope.reference.artifact_id,
            "scope_created_at": scope.created_at,
            "gate_name": gate_name,
            "evidence_path": evidence_path,
            "passed": True,
        },
        authorization=authorization,
    )
    return load_stable_artifact(
        artifact_root,
        artifact_id=artifact_id,
        artifact_kind="readiness_gate",
        semantic_version="1.0",
    )


def write_implementation_readiness(
    artifact_root: str | Path,
    *,
    scope: StableArtifact,
    gate_artifact_ids: tuple[str, ...],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Assemble exactly twenty scope-bound gates."""

    if type(scope) is not StableArtifact:
        raise ValueError("readiness scope is invalid")
    if type(gate_artifact_ids) is not tuple or len(gate_artifact_ids) != len(
        REQUIRED_READINESS_GATES
    ):
        raise ValueError("readiness gate IDs are incomplete")
    gates = tuple(
        load_stable_artifact(
            artifact_root,
            artifact_id=artifact_id,
            artifact_kind="readiness_gate",
            semantic_version="1.0",
        )
        for artifact_id in gate_artifact_ids
    )
    _validate_gates(gates, scope)
    artifact_id = f"implementation_readiness/{scope.reference.artifact_kind}/current/v1"
    write_stable_artifact(
        artifact_root,
        artifact_id=artifact_id,
        artifact_kind="implementation_readiness",
        semantic_version="1.0",
        created_at=_now(),
        inputs=(scope.reference, *(gate.reference for gate in gates)),
        payload={
            "scope_artifact_id": scope.reference.artifact_id,
            "scope_created_at": scope.created_at,
            "gate_artifact_ids": list(gate_artifact_ids),
            "gate_names": list(REQUIRED_READINESS_GATES),
            "complete": True,
        },
        authorization=authorization,
    )
    return load_implementation_readiness(
        artifact_root,
        scope=scope,
        artifact_id=artifact_id,
    )


def load_implementation_readiness(
    artifact_root: str | Path,
    *,
    scope: StableArtifact,
    artifact_id: str,
) -> StableArtifact:
    """Reload every gate and verify its exact scope reference."""

    readiness = load_stable_artifact(
        artifact_root,
        artifact_id=artifact_id,
        artifact_kind="implementation_readiness",
        semantic_version="1.0",
    )
    payload = readiness.payload
    if (
        payload.get("scope_artifact_id") != scope.reference.artifact_id
        or payload.get("scope_created_at") != scope.created_at
        or payload.get("gate_names") != list(REQUIRED_READINESS_GATES)
        or payload.get("complete") is not True
    ):
        raise RuntimeError("implementation readiness scope is invalid")
    raw_ids = payload.get("gate_artifact_ids")
    if type(raw_ids) is not list or any(type(value) is not str for value in raw_ids):
        raise RuntimeError("implementation readiness gate IDs are invalid")
    gates = tuple(
        load_stable_artifact(
            artifact_root,
            artifact_id=artifact_id,
            artifact_kind="readiness_gate",
            semantic_version="1.0",
        )
        for artifact_id in raw_ids
    )
    _validate_gates(gates, scope)
    expected_inputs = (scope.reference, *(gate.reference for gate in gates))
    if readiness.inputs != expected_inputs:
        raise RuntimeError("implementation readiness dependencies are invalid")
    return readiness


def _validate_gates(gates: tuple[StableArtifact, ...], scope: StableArtifact) -> None:
    names: list[str] = []
    for gate in gates:
        payload = gate.payload
        if (
            gate.inputs != (scope.reference,)
            or payload.get("scope_artifact_id") != scope.reference.artifact_id
            or payload.get("scope_created_at") != scope.created_at
            or payload.get("passed") is not True
            or type(payload.get("evidence_path")) is not str
        ):
            raise RuntimeError("readiness gate scope is invalid")
        name = payload.get("gate_name")
        if type(name) is not str:
            raise RuntimeError("readiness gate name is invalid")
        names.append(name)
    if tuple(names) != REQUIRED_READINESS_GATES:
        raise RuntimeError("readiness gate coverage is invalid")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = [
    "REQUIRED_READINESS_GATES",
    "load_implementation_readiness",
    "write_implementation_readiness",
    "write_readiness_gate",
]
