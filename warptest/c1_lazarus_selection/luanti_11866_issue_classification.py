"""Typed issue-classification revalidation for Luanti #11866.

The first atomicity receipt selected issue #11600 because it was the first
issue in the source manifest.  That issue is a release-planning Discussion,
not a defect report.  The same authoritative capture contains the explicit
cross-reference to #11839, whose bug report describes the out-of-bounds
entity/logging failure and later records that the failure no longer occurs by
default since #11866.  This module records that correction without rewriting
the source or atomicity artifacts and without changing the candidate roster.
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


_CANDIDATE = {"repository_key": "luanti", "change_number": 11866}
_AUTHORITY = {
    "api_base": "https://api.github.com",
    "provider": "github",
    "repository": "luanti-org/luanti",
}
_ENUMERATION_V2 = ArtifactReference(
    "repository_enumeration/luanti/current/v2", "repository_enumeration", "2.0"
)
_ENUMERATION_V1 = ArtifactReference(
    "repository_enumeration/luanti/current/v1", "repository_enumeration", "1.0"
)
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/11866/source/v2", "candidate_source_packet", "2.0"
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/11866/screening/v2", "candidate_test_patch", "2.0"
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/11866/preannotation/v2", "source_atomicity", "2.0"
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/11866/test_manifest/v2", "candidate_stage", "2.0"
)
_CHECKPOINT = ArtifactReference(
    "screening_checkpoint/luanti/11866/current/v2", "screening_checkpoint", "2.0"
)
_STALE_TERMINAL = ArtifactReference(
    "enumeration_terminal/luanti/11866/not-in-pinned/v1",
    "enumeration_terminal",
    "1.0",
)
_RECEIPT = ArtifactReference(
    "candidate_issue_classification/luanti/11866/preannotation/v1",
    "candidate_issue_classification",
    "1.0",
)

_PRODUCTION_PATHS = [
    "builtin/game/chat.lua",
    "src/constants.h",
    "src/defaultsettings.cpp",
    "src/map.cpp",
    "src/mapblock.h",
]
_TEST_PATHS = ["src/unittest/test_map.cpp", "src/unittest/test_player.cpp"]


def write_luanti_11866_issue_classification(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Reload the current chain and persist the issue reclassification receipt."""

    _require_authorization(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    source = _load(root, _SOURCE)
    patch = _load(root, _PATCH)
    atomicity = _load(root, _ATOMICITY)
    manifest = _load(root, _MANIFEST)
    checkpoint = _load(root, _CHECKPOINT)
    stale_terminal = _load(root, _STALE_TERMINAL)
    _validate_chain(source, patch, atomicity, manifest, checkpoint, stale_terminal)

    source_path = source.payload.get("raw_evidence_path")
    if not isinstance(source_path, str):
        raise RuntimeError("#11866 source evidence path is invalid")
    source_manifest = _read_json(root / _safe_relative(source_path))
    pull = source_manifest.get("pull")
    if not isinstance(pull, dict):
        raise RuntimeError("#11866 pull authority evidence is missing")
    issues = {
        number: issue
        for number in (11600, 11839)
        if (issue := _issue(source_manifest, number)) is not None
    }
    if set(issues) != {11600, 11839}:
        raise RuntimeError("#11866 expected both issue records")
    discussion = issues[11600]
    bug = issues[11839]
    _validate_issue_classification(
        discussion,
        bug,
        _issue_comments(source_manifest, 11839),
    )
    _validate_pull_and_scope(pull, patch, source_path, root)
    cross_reference_events = _cross_reference_events(source_manifest, {11600, 11839})
    if not {event["issue_number"] for event in cross_reference_events} >= {
        11600,
        11839,
    }:
        raise RuntimeError("#11866 cross-reference evidence is incomplete")

    payload: dict[str, Any] = {
        "candidate": _CANDIDATE,
        "state": "reclassified",
        "outcome": "issue_classified",
        "terminal": False,
        "resumable": True,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "classification": "bug_regression",
        "reclassified_from": [
            _STALE_TERMINAL.artifact_id,
            _ATOMICITY.artifact_id,
        ],
        "previous_screening": {
            "enumeration_reference": _ENUMERATION_V1.artifact_id,
            "terminal_reference": _STALE_TERMINAL.artifact_id,
            "terminal_reason": "repair_not_in_pinned",
            "current_enumeration_reference": _ENUMERATION_V2.artifact_id,
            "current_candidate_state": "patch_eligible",
        },
        "previous_primary_issue": {
            "number": 11600,
            "title": discussion["title"],
            "labels": _labels(discussion),
            "classification": "release_planning_discussion",
            "state": discussion["state"],
            "state_reason": discussion.get("state_reason"),
            "body_markers": [
                "## Background",
                "## Proposal",
                "releasing 5.5",
                "This would also mean prioritizing features",
            ],
        },
        "selected_primary_issue": {
            "number": 11839,
            "title": bug["title"],
            "labels": _labels(bug),
            "classification": "actual_bug_report",
            "state": bug["state"],
            "state_reason": bug.get("state_reason"),
            "body_markers": [
                "Summary",
                "maximum mapgen limit (31000)",
                "2.5 Megabytes",
                "Steps to reproduce",
            ],
            "closing_comment_marker": (
                "can not happen again with default settings since #11866"
            ),
        },
        "authority": _AUTHORITY,
        "source_manifest_path": source_path,
        "linked_issue_numbers": [11600, 11839],
        "cross_reference_events": cross_reference_events,
        "pull_scope": {
            "title": pull["title"],
            "merged": pull["merged"],
            "labels": _labels(pull),
            "body_markers": ["basic unittests included", "Ready for Review"],
            "changed_files": pull["changed_files"],
            "production_paths": _PRODUCTION_PATHS,
            "test_paths": _TEST_PATHS,
            "defect_surface": "mapgen and object-position boundary alignment",
            "single_defect_mapping": True,
        },
        "evidence_rationale": (
            "Issue #11600 is a Discussion about release scheduling and feature "
            "freeze. Issue #11839 is the concrete out-of-bounds entity/logging "
            "bug; its closing discussion explicitly attributes the default-settings "
            "fix to #11866. The PR changes the shared map-generation boundary and "
            "adds boundary assertions, so #11839 is the authoritative defect mapping."
        ),
        "gate_status": {
            "source_authority": "passed",
            "single_defect_mapping": "reclassified_to_issue_11839",
            "strict_inverse_patch": "not_evaluated",
            "runtime": "not_evaluated",
        },
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
        "next_action": "continue_current_v2_candidate_pipeline_without_inheriting_v1_terminal",
    }
    _assert_absent(root)
    inputs = (
        _ENUMERATION_V2,
        _SOURCE,
        _PATCH,
        _ATOMICITY,
        _MANIFEST,
        _CHECKPOINT,
        _STALE_TERMINAL,
    )
    write_stable_artifact(
        root,
        artifact_id=_RECEIPT.artifact_id,
        artifact_kind=_RECEIPT.artifact_kind,
        semantic_version=_RECEIPT.semantic_version,
        created_at=_now(),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(root, _RECEIPT)
    if artifact.inputs != inputs or artifact.payload != payload:
        raise RuntimeError("#11866 issue-classification receipt typed reload failed")
    return artifact


def _validate_chain(
    source: StableArtifact,
    patch: StableArtifact,
    atomicity: StableArtifact,
    manifest: StableArtifact,
    checkpoint: StableArtifact,
    stale_terminal: StableArtifact,
) -> None:
    if (
        source.inputs != (_ENUMERATION_V2,)
        or source.payload.get("candidate") != _CANDIDATE
        or source.payload.get("complete") is not True
        or patch.inputs != (_ENUMERATION_V2, _SOURCE)
        or patch.payload.get("candidate") != _CANDIDATE
        or patch.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
        or atomicity.inputs
        != (_ENUMERATION_V2, _SOURCE, _PATCH, _CHECKPOINT)
        or atomicity.payload.get("candidate") != _CANDIDATE
        or atomicity.payload.get("complete") is not True
        or atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("primary_issue_number") != 11600
        or atomicity.payload.get("linked_issue_numbers") != [11600, 11839]
        or manifest.payload.get("candidate") != _CANDIDATE
        or manifest.payload.get("complete") is not True
        or manifest.payload.get("stage") != "test_manifest"
        or manifest.payload.get("state") != "passed"
        or checkpoint.payload.get("candidate") != _CANDIDATE
        or checkpoint.payload.get("state") != "complete"
        or checkpoint.payload.get("stage_artifact_id") != _PATCH.artifact_id
        or stale_terminal.inputs != (_ENUMERATION_V1,)
        or stale_terminal.payload.get("candidate") != _CANDIDATE
        or stale_terminal.payload.get("reason") != "repair_not_in_pinned"
    ):
        raise RuntimeError("#11866 typed source/patch/atomicity chain is invalid")


def _validate_issue_classification(
    discussion: dict[str, Any],
    bug: dict[str, Any],
    bug_comments: list[dict[str, Any]],
) -> None:
    if (
        discussion.get("number") != 11600
        or discussion.get("title") != "5.5 release plans"
        or discussion.get("state") != "closed"
        or "Discussion" not in _labels(discussion)
        or "## Proposal" not in _body(discussion)
        or "releasing 5.5" not in _body(discussion)
        or bug.get("number") != 11839
        or bug.get("title")
        != "Node burning handler that does not delete node lags server, produces 2.5MB logs / second"
        or bug.get("state") != "closed"
        or "Unconfirmed bug" not in _labels(bug)
        or "Possible close" not in _labels(bug)
        or "maximum mapgen limit" not in _body(bug)
        or "Steps to reproduce" not in _body(bug)
        or not any(
            "can not happen again with default settings since #11866" in _body(comment)
            for comment in bug_comments
            if isinstance(comment, dict)
        )
    ):
        raise RuntimeError("#11866 issue-classification evidence is incomplete")


def _validate_pull_and_scope(
    pull: dict[str, Any],
    patch: StableArtifact,
    source_path: str,
    root: Path,
) -> None:
    patch_path = patch.payload.get("raw_patch_path")
    if (
        pull.get("number") != 11866
        or pull.get("merged") is not True
        or pull.get("title") != "Raise max mapgen limit constant to align with MapBlock"
        or "Bugfix 🐛" not in _labels(pull)
        or pull.get("changed_files") != 10
        or patch.payload.get("source_manifest_path") != source_path
        or patch.payload.get("production_paths") != _PRODUCTION_PATHS
        or patch.payload.get("test_paths") != _TEST_PATHS
        or not isinstance(patch_path, str)
    ):
        raise RuntimeError("#11866 pull/scope evidence is invalid")
    raw_patch = (root / _safe_relative(patch_path)).read_text(
        encoding="utf-8", errors="surrogateescape"
    )
    for marker in (
        "#define MAX_MAP_GENERATION_LIMIT (31007)",
        "const float max_limit_bs = (MAX_MAP_GENERATION_LIMIT + 0.5f) * BS;",
        "mapgen_limit (Map generation limit) int 31007 0 31007",
        "src/unittest/test_map.cpp",
    ):
        if marker not in raw_patch:
            raise RuntimeError(f"#11866 patch scope marker is missing: {marker}")


def _cross_reference_events(
    source_manifest: dict[str, Any], expected_numbers: set[int]
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for relationship in source_manifest.get("relationships", []):
        if not isinstance(relationship, dict) or relationship.get("event") != "cross-referenced":
            continue
        source = relationship.get("source")
        issue = source.get("issue") if isinstance(source, dict) else None
        number = issue.get("number") if isinstance(issue, dict) else None
        if number in expected_numbers:
            events.append(
                {
                    "event": "cross-referenced",
                    "issue_number": number,
                    "created_at": relationship.get("created_at"),
                    "actor": relationship.get("actor"),
                }
            )
    return sorted(events, key=lambda value: (value["issue_number"], value["created_at"] or ""))


def _issue(source_manifest: dict[str, Any], number: int) -> dict[str, Any] | None:
    for item in source_manifest.get("issues", []):
        if not isinstance(item, dict) or item.get("number") != number:
            continue
        issue = item.get("issue")
        if isinstance(issue, dict):
            return issue
    return None


def _issue_comments(source_manifest: dict[str, Any], number: int) -> list[dict[str, Any]]:
    for item in source_manifest.get("issues", []):
        if not isinstance(item, dict) or item.get("number") != number:
            continue
        comments = item.get("comments")
        if isinstance(comments, list) and all(isinstance(value, dict) for value in comments):
            return comments
    return []


def _labels(value: dict[str, Any]) -> list[str]:
    return [item["name"] for item in value.get("labels", []) if isinstance(item, dict) and isinstance(item.get("name"), str)]


def _body(value: dict[str, Any]) -> str:
    body = value.get("body")
    return body if isinstance(body, str) else ""


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("#11866 source manifest is invalid")
    return value


def _safe_relative(value: str) -> str:
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {".", ".."} for part in path.parts)
        or path.as_posix() != value
    ):
        raise RuntimeError("#11866 evidence path is unsafe")
    return path.as_posix()


def _require_authorization(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "candidate-screening"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#11866 issue classification requires candidate-screening authorization")


def _assert_absent(root: Path) -> None:
    path = root / "artifacts" / Path(_RECEIPT.artifact_id) / _RECEIPT.semantic_version
    if path.exists():
        raise RuntimeError("#11866 issue-classification receipt already exists")


def _now() -> str:
    return (datetime.now(UTC) + timedelta(microseconds=1)).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


__all__ = ["write_luanti_11866_issue_classification"]
