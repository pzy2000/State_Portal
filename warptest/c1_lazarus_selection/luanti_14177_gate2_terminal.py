"""Current-chain-bound Gate 2 terminal for Luanti #14177.

The test patch is mechanically usable, but the sole authoritative issue #8934
is a feature request for a new mod-loading-order capability.  This record
stops the candidate before Gate 4 or any native materialization/run.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = {"repository_key": "luanti", "change_number": 14177}
_ENUMERATION = ArtifactReference(
    "repository_enumeration/luanti/current/v2", "repository_enumeration", "2.0"
)
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/14177/source/v2", "candidate_source_packet", "2.0"
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/14177/screening/v2", "candidate_test_patch", "2.0"
)
_CHECKPOINT = ArtifactReference(
    "screening_checkpoint/luanti/14177/current/v2", "screening_checkpoint", "2.0"
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/14177/preannotation/v2", "source_atomicity", "2.0"
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/14177/test_manifest/v2", "candidate_stage", "2.0"
)
_TERMINAL = ArtifactReference(
    "candidate_gate14_terminal/luanti/14177/preannotation/v1",
    "candidate_gate14_terminal",
    "1.0",
)


def write_luanti_14177_gate2_terminal(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Write and strictly reload the feature-request terminal receipt."""

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "candidate-screening"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError(
            "#14177 terminal requires active candidate-screening authorization"
        )
    root = Path(artifact_root).absolute().resolve(strict=True)
    chain = tuple(
        _load(root, reference)
        for reference in (
            _ENUMERATION,
            _SOURCE,
            _PATCH,
            _CHECKPOINT,
            _ATOMICITY,
            _MANIFEST,
        )
    )
    _validate_chain(*chain)
    source_path = _relative(chain[1].payload.get("raw_evidence_path"), "source")
    source_manifest = _read_json(root / source_path)
    issue = _find_issue(source_manifest, 8934)
    pull = source_manifest.get("pull")
    if not isinstance(pull, dict):
        raise RuntimeError("#14177 pull evidence is invalid")
    issue_labels = _labels(issue)
    issue_body = issue.get("body")
    pull_body = pull.get("body")
    if (
        issue.get("title") != "Support for mod that is loaded before any other mod"
        or not isinstance(issue_body, str)
        or not isinstance(pull_body, str)
        or not {"Feature request", "Concept approved"}.issubset(issue_labels)
        or "## Solution" not in issue_body
        or "Add support for a special mod" not in issue_body
        or "Allow games to do something at the beginning and end" not in pull_body
    ):
        raise RuntimeError("#14177 feature-request classification evidence drifted")
    payload: dict[str, Any] = {
        "candidate": dict(_CANDIDATE),
        "state": "terminal",
        "outcome": "quality_rejected",
        "terminal": True,
        "resumable": False,
        "reason": "authoritative_feature_request_not_bug_or_regression",
        "classification": "new_mod_loading_order_capability",
        "primary_issue_number": 8934,
        "linked_issue_numbers": [8934, 14618],
        "authoritative_issue_count": 1,
        "pull_title": pull.get("title"),
        "issue_title": issue.get("title"),
        "issue_labels": sorted(issue_labels),
        "source_semantics": {
            "issue_is_feature_request": True,
            "issue_is_concept_approved": True,
            "issue_reports_preexisting_bug_or_regression": False,
            "issue_solution_adds_new_capability": True,
            "pr_implements_first_last_mod_configuration": True,
        },
        "gate_status": {
            "gate_1_source_authority": "passed",
            "gate_2_real_bug_or_regression_mapping": "failed_feature_request",
            "gate_3_production_only_inverse": "not_admitted_gate2_terminal",
            "gate_4_fixed_test_buggy_api_compatibility": "not_run_gate2_terminal",
            "gate_5_fixed_f2p_3x": "not_run_gate2_terminal",
            "gate_6_buggy_f2p_3x_failure": "not_run_gate2_terminal",
            "gate_7_fixed_buggy_p2p_3x": "not_run_gate2_terminal",
        },
        "candidate_materialization_performed": False,
        "candidate_execution_performed": False,
        "new_build_started": False,
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    inputs = tuple(item.reference for item in chain)
    try:
        existing = _load(root, _TERMINAL)
    except RuntimeError as error:
        if str(error) not in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            raise
    else:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError("#14177 terminal conflicts with current evidence")
        return existing
    write_stable_artifact(
        root,
        artifact_id=_TERMINAL.artifact_id,
        artifact_kind=_TERMINAL.artifact_kind,
        semantic_version=_TERMINAL.semantic_version,
        created_at=_now(),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(root, _TERMINAL)
    if artifact.inputs != inputs or dict(artifact.payload) != payload:
        raise RuntimeError("#14177 terminal typed reload failed")
    return artifact


def _validate_chain(
    enumeration: StableArtifact,
    source: StableArtifact,
    patch: StableArtifact,
    checkpoint: StableArtifact,
    atomicity: StableArtifact,
    manifest: StableArtifact,
) -> None:
    if enumeration.payload.get("repository_key") != "luanti":
        raise RuntimeError("#14177 enumeration is not Luanti")
    if (
        source.inputs != (_ENUMERATION,)
        or source.payload.get("candidate") != _CANDIDATE
        or source.payload.get("complete") is not True
        or patch.inputs != (_ENUMERATION, _SOURCE)
        or patch.payload.get("candidate") != _CANDIDATE
        or patch.payload.get("outcome") != "patch_eligible"
        or checkpoint.inputs != (_ENUMERATION, _PATCH)
        or checkpoint.payload.get("outcome") != "patch_eligible"
        or atomicity.inputs != (_ENUMERATION, _SOURCE, _PATCH, _CHECKPOINT)
        or atomicity.payload.get("candidate") != _CANDIDATE
        or atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("primary_issue_number") != 8934
        or atomicity.payload.get("authoritative_issue_numbers") != [8934]
        or manifest.inputs != (_PATCH,)
        or manifest.payload.get("candidate") != _CANDIDATE
        or manifest.payload.get("stage") != "test_manifest"
        or manifest.payload.get("state") != "passed"
        or manifest.payload.get("details", {}).get("executable_test_paths")
        != ["src/unittest/test_servermodmanager.cpp"]
    ):
        raise RuntimeError("#14177 current-v2 chain drifted")


def _relative(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"#14177 {label} path is invalid")
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or "." in candidate.parts or ".." in candidate.parts:
        raise RuntimeError(f"#14177 {label} path is unsafe")
    return Path(*candidate.parts)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("#14177 source evidence path is unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("#14177 source evidence is invalid") from error
    if not isinstance(value, dict) or value.get("candidate") != _CANDIDATE:
        raise RuntimeError("#14177 source evidence binding drifted")
    return value


def _find_issue(source_manifest: dict[str, Any], number: int) -> dict[str, Any]:
    for row in source_manifest.get("issues", []):
        if isinstance(row, dict) and row.get("number") == number:
            issue = row.get("issue")
            if isinstance(issue, dict):
                return issue
    raise RuntimeError(f"#14177 issue #{number} evidence is missing")


def _labels(value: dict[str, Any]) -> set[str]:
    labels = value.get("labels")
    if not isinstance(labels, list):
        raise RuntimeError("#14177 labels are invalid")
    return {
        item["name"]
        for item in labels
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["write_luanti_14177_gate2_terminal"]
