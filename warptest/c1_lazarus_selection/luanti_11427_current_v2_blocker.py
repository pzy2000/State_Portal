"""Typed Gate 2 blocker for Luanti #11427's exact current-v2 chain.

The PR body explicitly closes the circular-reference serialization defect
(#8719) and explicitly fixes the NaN/infinity serialization defect (#11899).
They are distinct bug reports with distinct failing inputs and semantics.  The
single-defect mapping cannot truthfully select one without a new scoped patch
and authority receipt, so this audit stops before Gate 3/4 and all execution.
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


_CANDIDATE = {"repository_key": "luanti", "change_number": 11427}
_ENUMERATION = ArtifactReference(
    "repository_enumeration/luanti/current/v2", "repository_enumeration", "2.0"
)
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/11427/source/v2", "candidate_source_packet", "2.0"
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/11427/screening/v2", "candidate_test_patch", "2.0"
)
_CHECKPOINT = ArtifactReference(
    "screening_checkpoint/luanti/11427/current/v2", "screening_checkpoint", "2.0"
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/11427/preannotation/v2", "source_atomicity", "2.0"
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/11427/test_manifest/v2", "candidate_stage", "2.0"
)
_CHAIN = (_ENUMERATION, _SOURCE, _PATCH, _CHECKPOINT, _ATOMICITY, _MANIFEST)
_BLOCKER = ArtifactReference(
    "candidate_gate2_atomicity_blocker/luanti/11427/current-v2/v1",
    "candidate_gate2_atomicity_blocker",
    "1.0",
)
_PRODUCTION_PATHS = ("builtin/common/serialize.lua",)
_TEST_PATHS = ("builtin/common/tests/serialize_spec.lua",)


def write_luanti_11427_current_v2_blocker(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Audit current proof only; write a resumable, no-execution blocker."""

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "infrastructure-audit"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError(
            "#11427 blocker requires active infrastructure-audit authorization"
        )
    root = Path(artifact_root).absolute().resolve(strict=True)
    chain = tuple(_load(root, reference) for reference in _CHAIN)
    _validate_chain(chain)
    source, patch, _checkpoint, atomicity, manifest = chain[1:]
    source_manifest_path = _relative(
        source.payload["raw_evidence_path"], "source manifest"
    )
    if (
        patch.payload.get("source_manifest_path") != source_manifest_path
        or atomicity.payload.get("source_manifest_path") != source_manifest_path
    ):
        raise RuntimeError("#11427 current-chain source binding drifted")
    source_audit = _audit_source(_read_json(root / source_manifest_path))

    payload: dict[str, Any] = {
        "candidate": dict(_CANDIDATE),
        "state": "blocked",
        "outcome": "atomicity_blocked",
        "terminal": False,
        "resumable": True,
        "candidate_eligibility": "not_admitted_pending_scoped_single_defect_mapping",
        "reason": "two_distinct_authoritative_bug_reports_explicitly_fixed_by_one_serializer_rewrite",
        "candidate_execution_performed": False,
        "candidate_materialization_performed": False,
        "new_build_started": False,
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "downstream_8_13_authorized": False,
        "current_gate_status": {
            "gate_1_source_authority": "passed",
            "gate_2_single_defect_mapping": "blocked_two_distinct_authoritative_bugs",
            "gate_3_strict_inverse_patch": "not_admissible_before_gate_2_scope_repair",
            "gate_4_fixed_test_api_compatibility": "not_admissible_before_gate_2_scope_repair",
            "gate_5_fixed_f2p_3x": "not_run",
            "gate_6_buggy_f2p_3x_failure": "not_run",
            "gate_7_p2p_3x_both_pass": "not_run",
        },
        "source_atomicity": {
            "receipt_outcome": atomicity.payload["outcome"],
            "reason": atomicity.payload["reason"],
            "authoritative_issue_numbers": list(
                atomicity.payload["authoritative_issue_numbers"]
            ),
            "linked_issue_numbers": list(atomicity.payload["linked_issue_numbers"]),
            "single_defect_mapping_admissible": False,
        },
        "authoritative_source_audit": source_audit,
        "source_patch_binding": {
            "source_manifest_path": source_manifest_path,
            "raw_patch_path": patch.payload["raw_patch_path"],
            "production_paths": list(_PRODUCTION_PATHS),
            "test_paths": list(_TEST_PATHS),
            "strict_inverse_patch": "not_admissible_before_gate_2_scope_repair",
        },
        "fresh_gate57_epoch": {
            "required_attempt_count": 12,
            "attempt_count": 0,
            "candidate_specific_runner_created": False,
            "inherit_prior_attempts": False,
            "old_attempts_referenced": False,
        },
        "next_action": (
            "do_not_reclassify_incidental_links; only resume after a registered "
            "one-defect scoped patch and atomicity receipt bind exactly one issue"
        ),
        "resource_policy": {
            "network": False,
            "paid": False,
            "formal_trials": False,
            "downstream_8_13": False,
        },
    }
    inputs = tuple(artifact.reference for artifact in chain)
    existing = _try_load(root)
    if existing is not None:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError("#11427 blocker conflicts with current typed evidence")
        return existing
    write_stable_artifact(
        root,
        artifact_id=_BLOCKER.artifact_id,
        artifact_kind=_BLOCKER.artifact_kind,
        semantic_version=_BLOCKER.semantic_version,
        created_at=_now(),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(root, _BLOCKER)
    if artifact.inputs != inputs or dict(artifact.payload) != payload:
        raise RuntimeError("#11427 blocker typed reload failed")
    return artifact


def _validate_chain(chain: tuple[StableArtifact, ...]) -> None:
    if tuple(artifact.reference for artifact in chain) != _CHAIN:
        raise RuntimeError("#11427 current-v2 chain identity drifted")
    if any(artifact.payload.get("candidate") != _CANDIDATE for artifact in chain[1:]):
        raise RuntimeError("#11427 current-v2 candidate binding drifted")
    enumeration, source, patch, checkpoint, atomicity, manifest = chain
    if enumeration.payload.get("repository_key") != "luanti":
        raise RuntimeError("#11427 enumeration is not Luanti")
    if source.inputs != (_ENUMERATION,) or source.payload.get("complete") is not True:
        raise RuntimeError("#11427 source packet is not complete")
    if (
        patch.inputs != (_ENUMERATION, _SOURCE)
        or patch.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
        or tuple(patch.payload.get("production_paths", ())) != _PRODUCTION_PATHS
        or tuple(patch.payload.get("test_paths", ())) != _TEST_PATHS
    ):
        raise RuntimeError("#11427 patch inventory drifted")
    if (
        checkpoint.inputs != (_ENUMERATION, _PATCH)
        or checkpoint.payload.get("state") != "complete"
        or checkpoint.payload.get("outcome") != "patch_eligible"
        or checkpoint.payload.get("stage_artifact_id") != _PATCH.artifact_id
    ):
        raise RuntimeError("#11427 screening checkpoint drifted")
    if (
        atomicity.inputs != (_ENUMERATION, _SOURCE, _PATCH, _CHECKPOINT)
        or atomicity.payload.get("complete") is not True
        or atomicity.payload.get("outcome") != "rejected"
        or atomicity.payload.get("reason") != "multiple_authoritative_issues"
        or atomicity.payload.get("authoritative_issue_numbers") != [8719, 11899]
        or atomicity.payload.get("linked_issue_numbers") != [7574, 8719, 11600, 11899]
    ):
        raise RuntimeError("#11427 source atomicity state changed")
    details = manifest.payload.get("details")
    if (
        manifest.inputs != (_PATCH,)
        or manifest.payload.get("complete") is not True
        or manifest.payload.get("stage") != "test_manifest"
        or manifest.payload.get("state") != "passed"
        or not isinstance(details, dict)
        or details.get("executable_test_paths") != list(_TEST_PATHS)
        or details.get("test_count") != 1
    ):
        raise RuntimeError("#11427 test manifest state changed")


def _audit_source(source_manifest: dict[str, Any]) -> dict[str, object]:
    issue_8719 = _find_issue(source_manifest, 8719)
    issue_11899 = _find_issue(source_manifest, 11899)
    pull = source_manifest.get("pull")
    if not isinstance(pull, dict):
        raise RuntimeError("#11427 pull is invalid")
    body_8719 = issue_8719.get("body")
    body_11899 = issue_11899.get("body")
    pull_body = pull.get("body")
    if (
        _labels(issue_8719) != {"Bug", "@ Builtin"}
        or _labels(issue_11899) != {"Bug", "@ Script API"}
        or not isinstance(body_8719, str)
        or not isinstance(body_11899, str)
        or not isinstance(pull_body, str)
        or "circular references" not in body_8719
        or "NaN or infinities" not in issue_11899.get("title", "")
        or "Closes #8719" not in pull_body
        or "fixes #11899" not in pull_body
    ):
        raise RuntimeError("#11427 authoritative issue classification drifted")
    return {
        "pull_number": pull.get("number"),
        "pull_title": pull.get("title"),
        "pull_labels": sorted(_labels(pull)),
        "pull_explicit_fix_claims": ["Closes #8719", "fixes #11899"],
        "issues": {
            "8719": {
                "title": issue_8719.get("title"),
                "labels": sorted(_labels(issue_8719)),
                "defect_class": "circular_reference_serialization_output",
                "bug_markers": ["circular references", "nil dereference"],
            },
            "11899": {
                "title": issue_11899.get("title"),
                "labels": sorted(_labels(issue_11899)),
                "defect_class": "nan_and_infinity_serialization_deserialization",
                "bug_markers": ["NaN", "infinities", "deserialize"],
            },
        },
        "incidental_link_classification": {
            "7574": "upstream_issue_not_claimed_closed_by_pr",
            "11600": "release_planning_discussion_not_claimed_fixed_by_pr",
        },
        "distinct_authoritative_defects": True,
    }


def _find_issue(source_manifest: dict[str, Any], number: int) -> dict[str, Any]:
    for item in source_manifest.get("issues", []):
        if (
            isinstance(item, dict)
            and item.get("number") == number
            and isinstance(item.get("issue"), dict)
        ):
            return item["issue"]
    raise RuntimeError(f"#11427 issue #{number} is missing")


def _labels(value: dict[str, Any]) -> set[str]:
    labels = value.get("labels")
    if not isinstance(labels, list):
        raise RuntimeError("#11427 issue labels are invalid")
    return {
        item["name"]
        for item in labels
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }


def _relative(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise RuntimeError(f"#11427 {label} path is invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {".", ".."} for part in path.parts)
        or path.as_posix() != value
    ):
        raise RuntimeError(f"#11427 {label} path is unsafe")
    return path.as_posix()


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("#11427 source manifest is unsafe")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("#11427 source manifest is invalid")
    return value


def _try_load(root: Path) -> StableArtifact | None:
    try:
        return _load(root, _BLOCKER)
    except RuntimeError as error:
        if str(error) in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            return None
        raise


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["write_luanti_11427_current_v2_blocker"]
