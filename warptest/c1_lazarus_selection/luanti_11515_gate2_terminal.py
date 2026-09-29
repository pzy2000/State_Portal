"""Current-chain-bound quality terminal for Luanti #11515.

The patch's only atomicity authority, #11600, is the 5.5 release-planning
Discussion.  It is not a bug report or regression and cannot admit this PR's
new ``MINETEST_MOD_PATH`` capability into the single-defect benchmark.
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


_CANDIDATE = {"repository_key": "luanti", "change_number": 11515}
_ENUMERATION = ArtifactReference(
    "repository_enumeration/luanti/current/v2", "repository_enumeration", "2.0"
)
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/11515/source/v2", "candidate_source_packet", "2.0"
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/11515/screening/v2", "candidate_test_patch", "2.0"
)
_CHECKPOINT = ArtifactReference(
    "screening_checkpoint/luanti/11515/current/v2", "screening_checkpoint", "2.0"
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/11515/preannotation/v2", "source_atomicity", "2.0"
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/11515/test_manifest/v2", "candidate_stage", "2.0"
)
_TERMINAL = ArtifactReference(
    "candidate_gate14_terminal/luanti/11515/preannotation/v1",
    "candidate_gate14_terminal",
    "1.0",
)


def write_luanti_11515_gate2_terminal(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Persist and strictly reload the Gate 2 release-planning terminal."""

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "candidate-screening"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError(
            "#11515 terminal requires active candidate-screening authorization"
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
    source_path = _safe_relative(chain[1].payload.get("raw_evidence_path"))
    manifest = _read_source_manifest(root / source_path)
    issue = _find_issue(manifest, 11600)
    pull = manifest.get("pull")
    if not isinstance(pull, dict):
        raise RuntimeError("#11515 pull evidence is invalid")
    labels = _labels(issue)
    issue_body = issue.get("body")
    pull_body = pull.get("body")
    pull_labels = _labels(pull)
    if (
        issue.get("title") != "5.5 release plans"
        or labels != {"Discussion"}
        or not isinstance(issue_body, str)
        or not isinstance(pull_body, str)
        or "## Proposal" not in issue_body
        or "feature freeze" not in issue_body
        or "#11576" not in issue_body
        or pull.get("title") != 'Add "MINETEST_MOD_PATH" environment variable'
        or "Feature ✨" not in pull_labels
        or "This patch adds an environment variable" not in pull_body
    ):
        raise RuntimeError("#11515 release-planning classification evidence drifted")
    payload: dict[str, Any] = {
        "candidate": dict(_CANDIDATE),
        "state": "terminal",
        "outcome": "quality_rejected",
        "terminal": True,
        "resumable": False,
        "reason": "authoritative_issue_is_release_planning_discussion_not_bug_or_regression",
        "classification": "new_environment_variable_feature",
        "primary_issue_number": 11600,
        "authoritative_issue_count": 1,
        "linked_issue_numbers": [11600],
        "issue_title": issue.get("title"),
        "issue_labels": sorted(labels),
        "pull_title": pull.get("title"),
        "pull_labels": sorted(pull_labels),
        "source_semantics": {
            "issue_is_release_planning_discussion": True,
            "issue_is_bug_or_regression": False,
            "issue_mentions_unrelated_issues": True,
            "pr_is_feature_labeled": True,
            "pr_adds_new_mod_path_environment_variable": True,
        },
        "gate_status": {
            "gate_1_source_authority": "passed",
            "gate_2_real_bug_or_regression_mapping": "failed_release_planning_discussion",
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
    inputs = tuple(artifact.reference for artifact in chain)
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
            raise RuntimeError("#11515 terminal conflicts with current evidence")
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
    result = _load(root, _TERMINAL)
    if result.inputs != inputs or dict(result.payload) != payload:
        raise RuntimeError("#11515 terminal typed reload failed")
    return result


def _validate_chain(
    enumeration: StableArtifact,
    source: StableArtifact,
    patch: StableArtifact,
    checkpoint: StableArtifact,
    atomicity: StableArtifact,
    test_manifest: StableArtifact,
) -> None:
    if (
        enumeration.payload.get("repository_key") != "luanti"
        or source.inputs != (_ENUMERATION,)
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
        or atomicity.payload.get("primary_issue_number") != 11600
        or atomicity.payload.get("authoritative_issue_numbers") != [11600]
        or test_manifest.inputs != (_PATCH,)
        or test_manifest.payload.get("candidate") != _CANDIDATE
        or test_manifest.payload.get("stage") != "test_manifest"
        or test_manifest.payload.get("state") != "passed"
        or test_manifest.payload.get("details", {}).get("executable_test_paths")
        != ["src/unittest/test_servermodmanager.cpp"]
    ):
        raise RuntimeError("#11515 current-v2 chain drifted")


def _safe_relative(value: object) -> Path:
    if not isinstance(value, str) or not value:
        raise RuntimeError("#11515 source evidence path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or "." in path.parts or ".." in path.parts:
        raise RuntimeError("#11515 source evidence path is unsafe")
    return Path(*path.parts)


def _read_source_manifest(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("#11515 source evidence is unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("#11515 source evidence is invalid") from error
    if not isinstance(value, dict) or value.get("candidate") != _CANDIDATE:
        raise RuntimeError("#11515 source evidence binding drifted")
    return value


def _find_issue(source_manifest: dict[str, Any], number: int) -> dict[str, Any]:
    for row in source_manifest.get("issues", []):
        if isinstance(row, dict) and row.get("number") == number:
            value = row.get("issue")
            if isinstance(value, dict):
                return value
    raise RuntimeError(f"#11515 issue #{number} evidence is missing")


def _labels(value: dict[str, Any]) -> set[str]:
    labels = value.get("labels")
    if not isinstance(labels, list):
        raise RuntimeError("#11515 labels are invalid")
    return {
        entry["name"]
        for entry in labels
        if isinstance(entry, dict) and isinstance(entry.get("name"), str)
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


__all__ = ["write_luanti_11515_gate2_terminal"]
