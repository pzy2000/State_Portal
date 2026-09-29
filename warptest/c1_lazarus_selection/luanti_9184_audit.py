"""Typed Gate1--4 audit for Luanti #9184.

The PR is an explicit maintenance/test-framework introduction, not a
single-defect repair.  Its inverse would restore inline self-tests rather
than reproduce a pre-existing bug, so it is terminal for C1 Lazarus.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from .stable_protocol import ArtifactReference, AuthorizationRecord, StableArtifact, load_stable_artifact, write_stable_artifact


_ENUMERATION = ArtifactReference("repository_enumeration/luanti/current/v2", "repository_enumeration", "2.0")
_SOURCE = ArtifactReference("candidate_source_packet/luanti/9184/source/v2", "candidate_source_packet", "2.0")
_PATCH = ArtifactReference("candidate_test_patch/luanti/9184/screening/v2", "candidate_test_patch", "2.0")
_ATOMICITY = ArtifactReference("source_atomicity/luanti/9184/preannotation/v2", "source_atomicity", "2.0")
_MANIFEST = ArtifactReference("candidate_stage/luanti/9184/test_manifest/v2", "candidate_stage", "2.0")
_CHECKPOINT = ArtifactReference("screening_checkpoint/luanti/9184/current/v2", "screening_checkpoint", "2.0")
_TERMINAL = ArtifactReference(
    "candidate_gate14_terminal/luanti/9184/preannotation/v1",
    "candidate_gate14_terminal",
    "1.0",
)


def audit_luanti_9184(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Reload the full typed chain and persist the feature/test terminal."""

    if authorization.scope != "candidate-screening":
        raise PermissionError("#9184 Gate1--4 audit requires candidate-screening authorization")
    if authorization.revoked or authorization.local_user_id != os.getuid():
        raise PermissionError("#9184 audit authorization is inactive")
    root = Path(artifact_root).absolute().resolve(strict=True)
    source = _load(root, _SOURCE)
    patch = _load(root, _PATCH)
    atomicity = _load(root, _ATOMICITY)
    manifest = _load(root, _MANIFEST)
    checkpoint = _load(root, _CHECKPOINT)
    source_manifest_path = source.payload.get("raw_evidence_path")
    if not isinstance(source_manifest_path, str):
        raise RuntimeError("#9184 source manifest path is invalid")
    source_manifest = _read_json(root / source_manifest_path)
    pull = source_manifest.get("pull")
    candidate = {"repository_key": "luanti", "change_number": 9184}
    if (
        source.inputs != (_ENUMERATION,)
        or source.payload.get("candidate") != candidate
        or source.payload.get("complete") is not True
        or source_manifest.get("candidate") != candidate
        or source_manifest.get("complete") is not True
        or source_manifest.get("provider") != "github"
        or source_manifest.get("authority") != {
            "api_base": "https://api.github.com",
            "provider": "github",
            "repository": "luanti-org/luanti",
        }
        or source_manifest.get("issue_numbers") != [5469]
        or not isinstance(pull, dict)
        or pull.get("number") != 9184
        or pull.get("merged") is not True
    ):
        raise RuntimeError("#9184 source authority chain is invalid")
    patch_path = patch.payload.get("raw_patch_path")
    if not isinstance(patch_path, str):
        raise RuntimeError("#9184 raw patch path is invalid")
    raw_patch = (root / _safe_relative(patch_path)).read_text(encoding="utf-8", errors="surrogateescape")
    if (
        patch.inputs != (_ENUMERATION, _SOURCE)
        or patch.payload.get("candidate") != candidate
        or patch.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
        or tuple(patch.payload.get("production_paths", ())) != ("builtin/common/misc_helpers.lua", "builtin/common/serialize.lua")
        or tuple(patch.payload.get("test_paths", ())) != ("builtin/common/tests/misc_helpers_spec.lua", "builtin/common/tests/serialize_spec.lua", "builtin/common/tests/vector_spec.lua")
        or patch.payload.get("source_manifest_path") != source_manifest_path
        or "-- Unit tests" not in raw_patch
        or "builtin/common/tests/misc_helpers_spec.lua" not in raw_patch
    ):
        raise RuntimeError("#9184 patch/test binding is invalid")
    if (
        atomicity.inputs != (_ENUMERATION, _SOURCE, _PATCH, _CHECKPOINT)
        or atomicity.payload.get("candidate") != candidate
        or atomicity.payload.get("complete") is not True
        or atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("authoritative_issue_count") != 1
        or atomicity.payload.get("authoritative_issue_numbers") != [5469]
        or atomicity.payload.get("primary_issue_number") != 5469
        or atomicity.payload.get("linked_issue_numbers") != [5469]
        or atomicity.payload.get("source_manifest_path") != source_manifest_path
    ):
        raise RuntimeError("#9184 single-issue mapping is invalid")
    details = manifest.payload.get("details")
    if (
        manifest.inputs != (_PATCH,)
        or manifest.payload.get("candidate") != candidate
        or manifest.payload.get("complete") is not True
        or manifest.payload.get("stage") != "test_manifest"
        or manifest.payload.get("state") != "passed"
        or not isinstance(details, dict)
        or details.get("executable_test_paths") != ["builtin/common/tests/misc_helpers_spec.lua", "builtin/common/tests/serialize_spec.lua", "builtin/common/tests/vector_spec.lua"]
        or details.get("test_count") != 3
    ):
        raise RuntimeError("#9184 test manifest is invalid")
    if checkpoint.payload.get("candidate") != candidate or checkpoint.payload.get("state") != "complete" or checkpoint.payload.get("stage_artifact_id") != _PATCH.artifact_id:
        raise RuntimeError("#9184 checkpoint is invalid")

    labels = [item.get("name") for item in pull.get("labels", []) if isinstance(item, dict)]
    body = pull.get("body") if isinstance(pull.get("body"), str) else ""
    if "Feature ✨" not in labels or "unit test framework" not in body or "Fixes #5469" not in body:
        raise RuntimeError("#9184 feature/test classification evidence is incomplete")
    _assert_absent(root)
    payload = {
        "candidate": candidate,
        "state": "terminal",
        "outcome": "quality_rejected",
        "terminal": True,
        "resumable": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "reason": "test_framework_maintenance_feature_not_single_defect_regression",
        "classification": "maintenance_test_framework_introduction",
        "primary_issue_number": 5469,
        "authoritative_issue_count": 1,
        "pull_title": pull.get("title"),
        "pull_labels": labels,
        "pull_body_markers": ["The goal of this PR is to add a unit test framework to builtin", "Fixes #5469"],
        "production_paths": ["builtin/common/misc_helpers.lua", "builtin/common/serialize.lua"],
        "test_paths": ["builtin/common/tests/misc_helpers_spec.lua", "builtin/common/tests/serialize_spec.lua", "builtin/common/tests/vector_spec.lua"],
        "test_semantics": "moves inline self-checks into a new Busted test suite; no pre-existing defect is repaired",
        "gate_status": {
            "source_authority": "passed",
            "single_defect_mapping": "passed",
            "strict_inverse_patch": "not_applicable_feature",
            "test_api_compatibility": "not_applicable_feature",
        },
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    write_stable_artifact(root, artifact_id=_TERMINAL.artifact_id, artifact_kind=_TERMINAL.artifact_kind, semantic_version=_TERMINAL.semantic_version, created_at=_now(), inputs=(_SOURCE, _PATCH, _ATOMICITY, _MANIFEST, _CHECKPOINT), payload=payload, authorization=authorization)
    artifact = _load(root, _TERMINAL)
    if artifact.payload != payload:
        raise RuntimeError("#9184 terminal typed reload failed")
    return artifact


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(root, artifact_id=reference.artifact_id, artifact_kind=reference.artifact_kind, semantic_version=reference.semantic_version)


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if type(value) is not dict:
        raise RuntimeError("#9184 source manifest is invalid")
    return value


def _safe_relative(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {".", ".."} for part in path.parts) or path.as_posix() != value:
        raise RuntimeError("#9184 path is unsafe")
    return path.as_posix()


def _assert_absent(root: Path) -> None:
    path = root / "artifacts" / Path(_TERMINAL.artifact_id) / _TERMINAL.semantic_version
    if path.exists():
        raise RuntimeError("#9184 typed terminal already exists")


def _now() -> str:
    return (datetime.now(UTC) + timedelta(microseconds=1)).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["audit_luanti_9184"]
