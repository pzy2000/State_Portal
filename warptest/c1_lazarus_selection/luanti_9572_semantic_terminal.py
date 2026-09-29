"""Candidate-specific semantic terminal for Luanti #9572.

The support-complete fresh Gate 5--7 epoch is retained as execution evidence,
but the authoritative issue and pull request are feature requests.  This
module records that Gate 2 conclusion without reusing any earlier attempt or
changing the execution aggregate.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from .luanti_9572_gate57 import (
    _ATOMICITY,
    _CHECKPOINT,
    _MANIFEST,
    _PATCH,
    _SOURCE,
)
from .luanti_9572_support_closure import _AGGREGATE, _CAPSULE, _PREPARATION, _PROFILE
from .stable_protocol import ArtifactReference, AuthorizationRecord, StableArtifact, load_stable_artifact, write_stable_artifact


_TERMINAL = ArtifactReference(
    "candidate_gate14_semantic_terminal/luanti/9572/preannotation/v1",
    "candidate_gate14_semantic_terminal",
    "1.0",
)


def write_luanti_9572_semantic_terminal(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Reload source evidence and write a Gate 2 feature-request terminal."""

    if authorization.scope != "candidate-screening":
        raise PermissionError("#9572 semantic terminal requires candidate-screening authorization")
    if authorization.revoked or authorization.local_user_id != os.getuid():
        raise PermissionError("#9572 semantic terminal authorization is inactive")
    root = Path(artifact_root).absolute().resolve(strict=True)
    source = _load(root, _SOURCE)
    patch = _load(root, _PATCH)
    atomicity = _load(root, _ATOMICITY)
    manifest = _load(root, _MANIFEST)
    checkpoint = _load(root, _CHECKPOINT)
    profile = _load(root, _PROFILE)
    preparation = _load(root, _PREPARATION)
    capsule = _load(root, _CAPSULE)
    aggregate = _load(root, _AGGREGATE)
    candidate = {"repository_key": "luanti", "change_number": 9572}
    if any(artifact.payload.get("candidate") != candidate for artifact in (source, patch, atomicity, manifest, checkpoint, profile, preparation, capsule, aggregate)):
        raise RuntimeError("#9572 semantic terminal candidate binding is invalid")
    if aggregate.payload.get("qualified") is not True or aggregate.payload.get("attempt_count") != 12:
        raise RuntimeError("#9572 semantic terminal requires the fresh qualified aggregate")
    source_path = source.payload.get("raw_evidence_path")
    if not isinstance(source_path, str):
        raise RuntimeError("#9572 semantic terminal source evidence path is invalid")
    source_manifest = _read_json(root / _safe_relative(source_path))
    pull = source_manifest.get("pull")
    if not isinstance(pull, dict) or pull.get("number") != 9572 or pull.get("merged") is not True:
        raise RuntimeError("#9572 semantic terminal pull evidence is invalid")
    pull_labels = [item.get("name") for item in pull.get("labels", []) if isinstance(item, dict)]
    pull_body = pull.get("body") if isinstance(pull.get("body"), str) else ""
    issue = _issue(source_manifest, 9798)
    issue_labels = [item.get("name") for item in issue.get("labels", []) if isinstance(item, dict)]
    issue_body = issue.get("body") if isinstance(issue.get("body"), str) else ""
    if "Feature ✨" not in pull_labels or "Feature request" not in issue_labels:
        raise RuntimeError("#9572 feature-request classification evidence is incomplete")
    _assert_absent(root)
    payload = {
        "candidate": candidate,
        "state": "terminal",
        "outcome": "quality_rejected",
        "terminal": True,
        "resumable": False,
        "candidate_execution_performed": True,
        "paid_actions_authorized": False,
        "reason": "authoritative_feature_request_not_single_defect_regression",
        "classification": "feature_request_capability_addition",
        "primary_issue_number": 9798,
        "authoritative_issue_count": 1,
        "pull_title": pull.get("title"),
        "pull_labels": pull_labels,
        "issue_labels": issue_labels,
        "source_body_markers": [
            "This PR adds three vector functions useful for rotating objects",
            "Write code using these functions",
            "Feature request",
        ],
        "source_semantics": {
            "pull_body_contains_capability_addition": "This PR adds three vector functions useful for rotating objects" in pull_body,
            "issue_body_contains_current_behavior_request": "Right now the camera is fixed" in issue_body,
            "authoritative_issue_is_feature_request": True,
        },
        "production_paths": ["builtin/common/vector.lua"],
        "test_paths": ["builtin/common/tests/vector_spec.lua"],
        "test_semantics": "fresh vector rotation tests validate newly introduced API behavior; they do not repair an authoritative pre-existing defect",
        "execution_evidence": {
            "aggregate_reference": aggregate.reference.artifact_id,
            "profile_reference": profile.reference.artifact_id,
            "preparation_reference": preparation.reference.artifact_id,
            "capsule_reference": capsule.reference.artifact_id,
            "fresh_attempt_count": 12,
            "fresh_aggregate_qualified": True,
        },
        "gate_status": {
            "source_authority": "passed",
            "single_defect_mapping": "failed_feature_request",
            "strict_inverse_patch": "not_applicable_feature",
            "test_api_compatibility": "not_applicable_feature",
            "fresh_gate57_execution": "passed_but_not_admitted",
        },
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    inputs = (_SOURCE, _PATCH, _ATOMICITY, _MANIFEST, _CHECKPOINT, profile.reference, preparation.reference, capsule.reference, aggregate.reference)
    write_stable_artifact(root, artifact_id=_TERMINAL.artifact_id, artifact_kind=_TERMINAL.artifact_kind, semantic_version=_TERMINAL.semantic_version, created_at=_now(), inputs=inputs, payload=payload, authorization=authorization)
    artifact = _load(root, _TERMINAL)
    if artifact.payload != payload or artifact.inputs != inputs:
        raise RuntimeError("#9572 semantic terminal typed reload failed")
    return artifact


def _issue(source_manifest: dict[str, object], number: int) -> dict[str, object]:
    for item in source_manifest.get("issues", []):
        if isinstance(item, dict) and item.get("number") == number and isinstance(item.get("issue"), dict):
            return item["issue"]
    raise RuntimeError("#9572 authoritative issue is missing")


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(root, artifact_id=reference.artifact_id, artifact_kind=reference.artifact_kind, semantic_version=reference.semantic_version)


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if type(value) is not dict:
        raise RuntimeError("#9572 semantic source manifest is invalid")
    return value


def _safe_relative(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {".", ".."} for part in path.parts) or path.as_posix() != value:
        raise RuntimeError("#9572 semantic source path is unsafe")
    return path.as_posix()


def _assert_absent(root: Path) -> None:
    path = root / "artifacts" / Path(_TERMINAL.artifact_id) / _TERMINAL.semantic_version
    if path.exists():
        raise RuntimeError("#9572 semantic terminal already exists")


def _now() -> str:
    return (datetime.now(UTC) + timedelta(microseconds=1)).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["write_luanti_9572_semantic_terminal"]
