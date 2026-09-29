"""Typed current-v2 Gate 1--4 terminal for Luanti #14685.

The authoritative issue and PR add animated glTF capability and are explicitly
labelled as a feature request/feature.  The frozen test also depends on a new
``simple_skin.gltf`` asset that is outside the typed test inventory.  This
receipt terminates admission before capsule construction or execution.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from .stable_protocol import ArtifactReference, AuthorizationRecord, StableArtifact, load_stable_artifact, write_stable_artifact


_CANDIDATE = {"repository_key": "luanti", "change_number": 14685}
_ENUMERATION = ArtifactReference("repository_enumeration/luanti/current/v2", "repository_enumeration", "2.0")
_SOURCE = ArtifactReference("candidate_source_packet/luanti/14685/source/v2", "candidate_source_packet", "2.0")
_PATCH = ArtifactReference("candidate_test_patch/luanti/14685/screening/v2", "candidate_test_patch", "2.0")
_ATOMICITY = ArtifactReference("source_atomicity/luanti/14685/preannotation/v2", "source_atomicity", "2.0")
_MANIFEST = ArtifactReference("candidate_stage/luanti/14685/test_manifest/v2", "candidate_stage", "2.0")
_CHECKPOINT = ArtifactReference("screening_checkpoint/luanti/14685/current/v2", "screening_checkpoint", "2.0")
_TERMINAL = ArtifactReference("candidate_gate14_terminal/luanti/14685/preannotation/v1", "candidate_gate14_terminal", "1.0")
_TEST_PATH = "src/unittest/test_irr_gltf_mesh_loader.cpp"
_TEST_ASSET = "games/devtest/mods/gltf/models/gltf_simple_skin.gltf"


def write_luanti_14685_gate14_terminal(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    _require_authorization(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    source, patch, atomicity, manifest, checkpoint = (
        _load(root, ref) for ref in (_SOURCE, _PATCH, _ATOMICITY, _MANIFEST, _CHECKPOINT)
    )
    _validate_chain(source, patch, atomicity, manifest, checkpoint)
    source_manifest = _read_json(_evidence_file(root, source.payload.get("raw_evidence_path"), "source manifest"))
    pull, issue = _validate_authority(source_manifest)
    raw_patch = _evidence_file(root, patch.payload.get("raw_patch_path"), "raw patch").read_text(encoding="utf-8", errors="surrogateescape")
    paths = tuple(line.split(" b/", 1)[1] for line in raw_patch.splitlines() if line.startswith("diff --git "))
    if len(paths) != 32 or _TEST_PATH not in paths or _TEST_ASSET not in paths:
        raise RuntimeError("#14685 raw patch inventory drifted")
    for marker in (
        'loadMesh(model_stem + "simple_skin.gltf")',
        "SECTION(\"simple skin\")",
        "getAllJoints()",
        "animated glTF",
    ):
        if marker not in raw_patch and marker not in str(pull.get("title")):
            raise RuntimeError(f"#14685 feature/test marker drifted: {marker}")
    declared = set(patch.payload["production_paths"] + patch.payload["test_paths"])
    omitted = tuple(path for path in paths if path not in declared)
    expected_omitted = (
        ".gitattributes",
        "doc/lua_api.md",
        "games/devtest/mods/gltf/LICENSE.md",
        _TEST_ASSET,
        "games/devtest/mods/gltf/models/gltf_spider_animated.gltf",
    )
    if omitted != expected_omitted:
        raise RuntimeError("#14685 unregistered support inventory drifted")
    for relative in (
        "artifacts/candidate_profile/luanti/14685",
        "artifacts/fixed_source_capsule/luanti/14685",
        "artifacts/candidate_preparation/luanti/14685",
    ):
        if (root / relative).exists():
            raise RuntimeError("#14685 downstream namespace appeared; re-audit required")

    payload: dict[str, Any] = {
        "candidate": _CANDIDATE,
        "state": "terminal",
        "outcome": "quality_rejected",
        "terminal": True,
        "resumable": False,
        "reason": "authoritative_feature_request_not_single_defect_regression",
        "classification": "animated_gltf_capability_addition",
        "primary_issue_number": 9673,
        "authoritative_issue_count": 1,
        "pull_title": pull["title"],
        "pull_labels": _labels(pull),
        "issue_title": issue["title"],
        "issue_labels": _labels(issue),
        "current_gate_status": {
            "source_authority": "passed",
            "mechanical_single_issue_atomicity": "passed",
            "single_defect_mapping": "failed_feature_request_capability_addition",
            "strict_inverse_patch": "not_applicable_feature",
            "test_api_compatibility": "not_applicable_feature",
            "test_asset_binding": "blocked_unregistered_fixed_only_test_asset",
            "fresh_gate57_epoch": "blocked_before_materialization",
        },
        "patch_audit": {
            "declared_production_path_count": len(patch.payload["production_paths"]),
            "declared_test_paths": [_TEST_PATH],
            "raw_patch_path_count": len(paths),
            "unregistered_support_paths": list(omitted),
            "required_test_asset": _TEST_ASSET,
            "required_test_asset_registered": False,
            "test_asset_added_by_fixed_patch": True,
        },
        "fresh_epoch": {"required_attempt_count": 12, "attempt_count": 0, "inherit_prior_attempts": False, "old_attempts_loaded": False},
        "candidate_execution_performed": False,
        "candidate_materialization_performed": False,
        "new_build_started": False,
        "roster_modified": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "complete": True,
    }
    inputs = (_SOURCE, _PATCH, _ATOMICITY, _MANIFEST, _CHECKPOINT)
    existing = _try_load(root)
    if existing is not None:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError("#14685 existing terminal drifted")
        return existing
    write_stable_artifact(root, artifact_id=_TERMINAL.artifact_id, artifact_kind=_TERMINAL.artifact_kind, semantic_version=_TERMINAL.semantic_version, created_at=_now(), inputs=inputs, payload=payload, authorization=authorization)
    artifact = _load(root, _TERMINAL)
    if artifact.inputs != inputs or dict(artifact.payload) != payload:
        raise RuntimeError("#14685 terminal typed reload failed")
    return artifact


def _validate_chain(source: StableArtifact, patch: StableArtifact, atomicity: StableArtifact, manifest: StableArtifact, checkpoint: StableArtifact) -> None:
    if any(a.payload.get("candidate") != _CANDIDATE for a in (source, patch, atomicity, manifest, checkpoint)):
        raise RuntimeError("#14685 candidate binding drifted")
    details = manifest.payload.get("details")
    if (
        source.inputs != (_ENUMERATION,) or source.payload.get("complete") is not True
        or patch.inputs != (_ENUMERATION, _SOURCE) or patch.payload.get("outcome") != "patch_eligible"
        or len(patch.payload.get("production_paths", ())) != 26 or patch.payload.get("test_paths") != [_TEST_PATH]
        or checkpoint.inputs != (_ENUMERATION, _PATCH) or checkpoint.payload.get("outcome") != "patch_eligible"
        or atomicity.inputs != (_ENUMERATION, _SOURCE, _PATCH, _CHECKPOINT) or atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("primary_issue_number") != 9673 or atomicity.payload.get("authoritative_issue_numbers") != [9673]
        or manifest.inputs != (_PATCH,) or manifest.payload.get("state") != "passed"
        or type(details) is not dict or details.get("executable_test_paths") != [_TEST_PATH] or details.get("test_count") != 1
    ):
        raise RuntimeError("#14685 current-v2 chain drifted")


def _validate_authority(source_manifest: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    pull = source_manifest.get("pull")
    rows = source_manifest.get("issues")
    row = next((r for r in rows if r.get("number") == 9673), None) if type(rows) is list else None
    issue = row.get("issue") if type(row) is dict else None
    if (
        source_manifest.get("issue_numbers") != [9673] or type(pull) is not dict or pull.get("number") != 14685 or pull.get("merged") is not True
        or type(issue) is not dict or "Feature ✨" not in _labels(pull) or "Feature request" not in _labels(issue)
        or "Closes #9673" not in str(pull.get("body")) or "animated glTF" not in str(pull.get("title"))
    ):
        raise RuntimeError("#14685 feature authority drifted")
    return pull, issue


def _labels(value: dict[str, Any]) -> list[str]:
    return [x["name"] for x in value.get("labels", []) if type(x) is dict and type(x.get("name")) is str]


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if type(value) is not dict:
        raise RuntimeError("#14685 source manifest is invalid")
    return value


def _evidence_file(root: Path, value: object, label: str) -> Path:
    if type(value) is not str or not value:
        raise RuntimeError(f"#14685 {label} path is invalid")
    relative = PurePosixPath(value)
    if relative.is_absolute() or "." in relative.parts or ".." in relative.parts:
        raise RuntimeError(f"#14685 {label} path is unsafe")
    path = root.joinpath(*relative.parts)
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"#14685 {label} file is unsafe")
    return path


def _load(root: Path, ref: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(root, artifact_id=ref.artifact_id, artifact_kind=ref.artifact_kind, semantic_version=ref.semantic_version)


def _try_load(root: Path) -> StableArtifact | None:
    try:
        return _load(root, _TERMINAL)
    except RuntimeError as error:
        if str(error) in {"artifact directory is missing", "artifact current head is missing"}:
            return None
        raise


def _require_authorization(value: AuthorizationRecord) -> None:
    if type(value) is not AuthorizationRecord or value.scope != "candidate-screening" or value.revoked or value.local_user_id != os.getuid():
        raise PermissionError("#14685 terminal requires candidate-screening authorization")


def _now() -> str:
    return (datetime.now(UTC) + timedelta(microseconds=1)).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["write_luanti_14685_gate14_terminal"]
