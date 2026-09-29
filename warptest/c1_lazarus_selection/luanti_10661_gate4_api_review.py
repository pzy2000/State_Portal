"""Evidence-only Gate 4 API review for Luanti #10661.

Issue #10555 is a real regression and the current-v2 authority chain has one
authoritative issue.  The pull request, however, changes the
``MapSettingsManager`` constructor while changing the frozen test to call the
new one-argument form.  Reversing only production would leave that test
calling a fixed-only API.  This is a mechanical compatibility check that has
not yet been run against a candidate-specific capsule; it is deliberately a
resumable candidate-pool state, not a semantic rejection.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = {"repository_key": "luanti", "change_number": 10661}
_ENUMERATION = ArtifactReference(
    "repository_enumeration/luanti/current/v2", "repository_enumeration", "2.0"
)
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/10661/source/v2", "candidate_source_packet", "2.0"
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/10661/screening/v2", "candidate_test_patch", "2.0"
)
_CHECKPOINT = ArtifactReference(
    "screening_checkpoint/luanti/10661/current/v2", "screening_checkpoint", "2.0"
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/10661/preannotation/v2", "source_atomicity", "2.0"
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/10661/test_manifest/v2", "candidate_stage", "2.0"
)
_REVIEW = ArtifactReference(
    "candidate_gate4_api_review/luanti/10661/current-v2/v1",
    "candidate_gate4_api_review",
    "1.0",
)


def write_luanti_10661_gate4_api_review(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Write a typed, nonterminal API-risk review without executing the candidate."""

    _require_authorization(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    source = _load(root, _SOURCE)
    patch = _load(root, _PATCH)
    checkpoint = _load(root, _CHECKPOINT)
    atomicity = _load(root, _ATOMICITY)
    manifest = _load(root, _MANIFEST)
    _validate_chain(source, patch, checkpoint, atomicity, manifest)
    source_manifest_path = _safe_path(root, source.payload.get("raw_evidence_path"))
    patch_path = _safe_path(root, patch.payload.get("raw_patch_path"))
    source_manifest = _read_json(source_manifest_path)
    raw_patch = patch_path.read_text(encoding="utf-8", errors="surrogateescape")
    _validate_authority(source_manifest)
    observations = _api_observations(raw_patch)
    payload: dict[str, Any] = {
        "candidate": _CANDIDATE,
        "state": "candidate_pool_reopened_pending_gate4_api_check",
        "outcome": "gate4_mechanical_api_check_pending",
        "terminal": False,
        "resumable": True,
        "candidate_pool_status": "eligible_for_candidate_specific_gate4_recheck",
        "mechanical_api_check_performed": False,
        "candidate_execution_performed": False,
        "old_attempts_loaded": False,
        "source_manifest_path": source.payload["raw_evidence_path"],
        "raw_patch_path": patch.payload["raw_patch_path"],
        "authority": {
            "primary_issue_number": 10555,
            "authoritative_issue_numbers": [10555],
            "classification": "real_bug_regression",
            "issue_title": "Mapgen settings in a game's minetest.conf get ignored",
        },
        "api_compatibility_observation": observations,
        "gate_status": {
            "gate_1_source_authority": "passed",
            "gate_2_single_defect_mapping": "accepted_issue_10555",
            "gate_3_strict_inverse_patch": "pending_candidate_capsule_check",
            "gate_4_test_api_compatibility": "pending_mechanical_check",
            "gate_5_7": "not_run",
            "gate_8_13": "not_claimed",
        },
        "risk_statement": (
            "The fixed test calls the one-argument MapSettingsManager constructor, "
            "while reversing production restores the two-argument buggy API. "
            "This is a mechanical Gate 4 risk, not a semantic issue rejection."
        ),
        "next_action": (
            "materialize candidate-specific fixed capsule, reverse only production "
            "paths, and run the frozen-test-against-buggy-API mechanical checker"
        ),
        "roster_modified": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
        "complete": True,
    }
    _assert_absent(root)
    inputs = (_ENUMERATION, _SOURCE, _PATCH, _CHECKPOINT, _ATOMICITY, _MANIFEST)
    write_stable_artifact(
        root,
        artifact_id=_REVIEW.artifact_id,
        artifact_kind=_REVIEW.artifact_kind,
        semantic_version=_REVIEW.semantic_version,
        created_at=_now(),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(root, _REVIEW)
    if artifact.inputs != inputs or artifact.payload != payload:
        raise RuntimeError("#10661 Gate 4 API review typed reload failed")
    return artifact


def _validate_chain(
    source: StableArtifact,
    patch: StableArtifact,
    checkpoint: StableArtifact,
    atomicity: StableArtifact,
    manifest: StableArtifact,
) -> None:
    if (
        source.inputs != (_ENUMERATION,)
        or source.payload.get("candidate") != _CANDIDATE
        or source.payload.get("complete") is not True
        or patch.inputs != (_ENUMERATION, _SOURCE)
        or patch.payload.get("candidate") != _CANDIDATE
        or patch.payload.get("outcome") != "patch_eligible"
        or checkpoint.inputs != (_ENUMERATION, _PATCH)
        or checkpoint.payload.get("candidate") != _CANDIDATE
        or checkpoint.payload.get("outcome") != "patch_eligible"
        or atomicity.inputs != (_ENUMERATION, _SOURCE, _PATCH, _CHECKPOINT)
        or atomicity.payload.get("candidate") != _CANDIDATE
        or atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("primary_issue_number") != 10555
        or atomicity.payload.get("authoritative_issue_numbers") != [10555]
        or manifest.inputs != (_PATCH,)
        or manifest.payload.get("candidate") != _CANDIDATE
        or manifest.payload.get("state") != "passed"
    ):
        raise RuntimeError("#10661 current-v2 Gate 1-4 chain drifted")


def _validate_authority(source_manifest: dict[str, Any]) -> None:
    issues = source_manifest.get("issues")
    pull = source_manifest.get("pull")
    if not isinstance(issues, list) or not isinstance(pull, dict):
        raise RuntimeError("#10661 source authority evidence is malformed")
    issue = next(
        (
            row.get("issue")
            for row in issues
            if isinstance(row, dict) and row.get("number") == 10555
        ),
        None,
    )
    if not isinstance(issue, dict):
        raise RuntimeError("#10661 issue #10555 is missing")
    labels = [
        value.get("name")
        for value in issue.get("labels", [])
        if isinstance(value, dict) and isinstance(value.get("name"), str)
    ]
    body = issue.get("body") if isinstance(issue.get("body"), str) else ""
    if (
        issue.get("title") != "Mapgen settings in a game's minetest.conf get ignored"
        or "Bug" not in labels
        or "minetest.conf" not in body
        or pull.get("number") != 10661
        or pull.get("merged") is not True
    ):
        raise RuntimeError("#10661 issue/PR authority evidence drifted")


def _api_observations(raw_patch: str) -> dict[str, Any]:
    lines = raw_patch.splitlines()
    markers = {
        "fixed_test_constructor": "MapSettingsManager mgr(test_mapmeta_path);",
        "buggy_test_constructor": "MapSettingsManager mgr(&user_settings, test_mapmeta_path);",
        "fixed_header_constructor": "MapSettingsManager(const std::string &map_meta_path);",
        "buggy_header_constructor": "MapSettingsManager(Settings *user_settings,",
        "fixed_cpp_constructor": "MapSettingsManager::MapSettingsManager(const std::string &map_meta_path):",
        "buggy_cpp_constructor": "MapSettingsManager::MapSettingsManager(Settings *user_settings,",
    }
    found: dict[str, list[int]] = {}
    for name, marker in markers.items():
        found[name] = [index for index, line in enumerate(lines, 1) if marker in line]
        if not found[name]:
            raise RuntimeError(f"#10661 API marker missing: {name}")
    return {
        "production_paths_reversed_for_check": [
            "src/map_settings_manager.cpp",
            "src/map_settings_manager.h",
        ],
        "frozen_test_paths": ["src/unittest/test_map_settings_manager.cpp"],
        "fixed_only_signature_risk": True,
        "patch_line_markers": found,
        "observation_status": "static_risk_detected_mechanical_check_not_run",
    }


def _safe_path(root: Path, value: object) -> Path:
    if not isinstance(value, str) or not value:
        raise RuntimeError("#10661 evidence path is invalid")
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(part in {".", ".."} for part in relative.parts):
        raise RuntimeError("#10661 evidence path is unsafe")
    path = root.joinpath(*relative.parts)
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("#10661 evidence file is unavailable")
    return path


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("#10661 source manifest is malformed")
    return value


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _assert_absent(root: Path) -> None:
    path = root / "artifacts" / Path(_REVIEW.artifact_id) / _REVIEW.semantic_version
    if path.exists():
        raise RuntimeError("#10661 Gate 4 API review already exists")


def _require_authorization(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "candidate-screening"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError(
            "#10661 API review requires candidate-screening authorization"
        )


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["write_luanti_10661_gate4_api_review"]
