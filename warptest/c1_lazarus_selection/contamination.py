"""Per-instance public-source contamination reporting."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from .models import CandidateKey
from .stable_protocol import (
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_TOKEN = re.compile(r"[a-z0-9]+")


def write_contamination_record(
    artifact_root: str | Path,
    *,
    repository_key: str,
    change_number: int,
    exact_text_readily_searchable: bool,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Derive source-text overlap while preserving explicit exposure limits."""

    if type(exact_text_readily_searchable) is not bool:
        raise ValueError("contamination searchability state is invalid")
    candidate = CandidateKey(repository_key, change_number)
    source = load_stable_artifact(
        artifact_root,
        artifact_id=f"candidate_source_packet/{repository_key}/{change_number}/source/v1",
        artifact_kind="candidate_source_packet",
        semantic_version="1.0",
    )
    task = load_stable_artifact(
        artifact_root,
        artifact_id=f"public_task/{repository_key}/{change_number}/current/v1",
        artifact_kind="public_task",
        semantic_version="1.0",
    )
    for artifact, label in ((source, "source"), (task, "task")):
        if (
            artifact.payload.get("candidate") != candidate.to_value()
            or artifact.payload.get("complete") is not True
        ):
            raise RuntimeError(f"contamination {label} artifact is invalid")
    public_text = source.payload.get("public_text")
    task_text = task.payload.get("task_text")
    if type(public_text) is not str or type(task_text) is not str:
        raise RuntimeError("contamination text evidence is missing")
    source_tokens = frozenset(_TOKEN.findall(public_text.casefold()))
    task_tokens = frozenset(_TOKEN.findall(task_text.casefold()))
    union = source_tokens | task_tokens
    overlap = len(source_tokens & task_tokens) / len(union) if union else 0.0
    payload = {
        "candidate": candidate.to_value(),
        "issue_created_at": _required_text(source, "issue_created_at"),
        "change_created_at": _required_text(source, "change_created_at"),
        "issue_url": _required_text(source, "issue_url"),
        "change_url": _required_text(source, "change_url"),
        "exact_text_readily_searchable": exact_text_readily_searchable,
        "source_text_overlap": overlap,
        "risk_statement": "public_training_exposure_possible",
        "complete": True,
    }
    artifact_id = f"contamination_record/{repository_key}/{change_number}/current/v1"
    write_stable_artifact(
        artifact_root,
        artifact_id=artifact_id,
        artifact_kind="contamination_record",
        semantic_version="1.0",
        created_at=_now(),
        inputs=(source.reference, task.reference),
        payload=payload,
        authorization=authorization,
    )
    loaded = load_stable_artifact(
        artifact_root,
        artifact_id=artifact_id,
        artifact_kind="contamination_record",
        semantic_version="1.0",
    )
    if loaded.inputs != (source.reference, task.reference) or loaded.payload != payload:
        raise RuntimeError("contamination record reload is inconsistent")
    return loaded


def _required_text(artifact: StableArtifact, key: str) -> str:
    value = artifact.payload.get(key)
    if type(value) is not str or not value:
        raise RuntimeError(f"contamination source {key} is missing")
    return value


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["write_contamination_record"]
