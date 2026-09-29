"""Narrow current-v2 authority re-audit for Luanti #12595.

The old atomicity rejection listed #13486 and #14210.  This audit checks
whether the latter is merely an incidental cross-reference.  It is not: the
merged PR body explicitly says ``Fixes #13486, fixes #14210``; #14210 is a
separate report about per-action moved counts (rather than swap callback
ordering) and was closed with the PR merge.  No Gate 3--7 work is admitted.
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


_CANDIDATE = {"repository_key": "luanti", "change_number": 12595}
_ENUMERATION = ArtifactReference(
    "repository_enumeration/luanti/current/v2", "repository_enumeration", "2.0"
)
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/12595/source/v2", "candidate_source_packet", "2.0"
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/12595/screening/v2", "candidate_test_patch", "2.0"
)
_CHECKPOINT = ArtifactReference(
    "screening_checkpoint/luanti/12595/current/v2", "screening_checkpoint", "2.0"
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/12595/preannotation/v2", "source_atomicity", "2.0"
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/12595/test_manifest/v2", "candidate_stage", "2.0"
)
_OUTPUT = ArtifactReference(
    "candidate_gate2_authority_reaudit/luanti/12595/current-v2/v2",
    "candidate_gate2_authority_reaudit",
    "1.0",
)


def write_luanti_12595_authority_reaudit(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Write and reload the exact-chain multi-authority conclusion."""

    _require_authorization(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    chain = tuple(
        _load(root, reference)
        for reference in (_ENUMERATION, _SOURCE, _PATCH, _CHECKPOINT, _ATOMICITY, _MANIFEST)
    )
    _validate_chain(chain)
    source, patch = chain[1], chain[2]
    manifest = _read_json(root / _relative(source.payload.get("raw_evidence_path"), "source manifest"))
    raw_patch = _read_bytes(root / _relative(patch.payload.get("raw_patch_path"), "raw patch"))
    pull, issue_13486, issue_14210 = _audit_sources(manifest, raw_patch)
    _assert_absent(root)
    payload: dict[str, Any] = {
        "candidate": dict(_CANDIDATE),
        "state": "terminal",
        "outcome": "multiple_distinct_authoritative_defects_confirmed",
        "terminal": True,
        "resumable": False,
        "reason": "two_explicitly_fixed_issues_have_distinct_observable_failure_contracts",
        "mechanical_atomicity": {
            "linked_issue_numbers": [13486, 14210],
            "original_authoritative_issue_numbers": [13486, 14210],
            "outcome": "rejected",
            "reason": "multiple_authoritative_issues",
        },
        "authority_reaudit": {
            "primary_candidate_issue": {
                "number": 13486,
                "title": issue_13486.get("title"),
                "observable_contract": "swap_callbacks_take_before_put",
                "pr_linkage": "explicit_fixes_in_merged_pr_body",
            },
            "second_issue": {
                "number": 14210,
                "title": issue_14210.get("title"),
                "observable_contract": "register_on_player_inventory_action_count_matches_individual_shift_click_transactions",
                "pr_linkage": "explicit_fixes_in_merged_pr_body_and_closed_with_merge",
                "incidental_cross_reference": False,
                "why_not_incidental": [
                    "merged_pr_body_explicitly_says_fixes_14210",
                    "issue_14210_closed_at_2024_03_16T19_14_06Z_after_pr_merge_at_2024_03_16T19_14_05Z",
                    "test_and_patch_touch_move_count_separately_from_callback_order",
                ],
            },
            "pull_title": pull.get("title"),
            "strict_single_defect_mapping": False,
        },
        "gate_status": {
            "gate_1_source_authority": "passed",
            "gate_2_real_single_bug_or_regression_mapping": "failed_multiple_authoritative_defects",
            "gate_3_production_only_inverse": "not_admissible_before_gate2",
            "gate_4_fixed_test_buggy_api_compatibility": "not_admissible_before_gate2",
            "gate_5_7": "not_run_gate2_terminal",
        },
        "candidate_execution_performed": False,
        "candidate_materialization_performed": False,
        "new_build_started": False,
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    inputs = tuple(artifact.reference for artifact in chain)
    write_stable_artifact(
        root,
        artifact_id=_OUTPUT.artifact_id,
        artifact_kind=_OUTPUT.artifact_kind,
        semantic_version=_OUTPUT.semantic_version,
        created_at=_now(),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(root, _OUTPUT)
    if artifact.inputs != inputs or dict(artifact.payload) != payload:
        raise RuntimeError("#12595 authority re-audit typed reload failed")
    return artifact


def _validate_chain(chain: tuple[StableArtifact, ...]) -> None:
    enumeration, source, patch, checkpoint, atomicity, manifest = chain
    if enumeration.payload.get("repository_key") != "luanti":
        raise RuntimeError("#12595 enumeration binding is invalid")
    if (
        source.inputs != (_ENUMERATION,)
        or source.payload.get("candidate") != _CANDIDATE
        or source.payload.get("complete") is not True
        or patch.inputs != (_ENUMERATION, _SOURCE)
        or patch.payload.get("candidate") != _CANDIDATE
        or patch.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
        or checkpoint.inputs != (_ENUMERATION, _PATCH)
        or checkpoint.payload.get("candidate") != _CANDIDATE
        or checkpoint.payload.get("state") != "complete"
        or atomicity.inputs != (_ENUMERATION, _SOURCE, _PATCH, _CHECKPOINT)
        or atomicity.payload.get("candidate") != _CANDIDATE
        or atomicity.payload.get("authoritative_issue_numbers") != [13486, 14210]
        or atomicity.payload.get("outcome") != "rejected"
        or atomicity.payload.get("reason") != "multiple_authoritative_issues"
        or manifest.inputs != (_PATCH,)
        or manifest.payload.get("candidate") != _CANDIDATE
        or manifest.payload.get("complete") is not True
        or manifest.payload.get("state") != "passed"
        or manifest.payload.get("details", {}).get("test_count") != 2
    ):
        raise RuntimeError("#12595 current-v2 chain drifted")


def _audit_sources(manifest: dict[str, Any], raw_patch: bytes) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if (
        manifest.get("candidate") != _CANDIDATE
        or manifest.get("complete") is not True
        or manifest.get("issue_numbers") != [13486, 14210]
        or not isinstance(manifest.get("pull"), dict)
    ):
        raise RuntimeError("#12595 raw source manifest binding drifted")
    pull = manifest["pull"]
    body = pull.get("body")
    if (
        pull.get("number") != 12595
        or pull.get("merged") is not True
        or pull.get("merged_at") != "2024-03-16T19:14:05Z"
        or not isinstance(body, str)
        or "Fixes #13486, fixes #14210" not in body
        or b"void IMoveAction::onTake" not in raw_patch
        or b"move_count = src_item.count" not in raw_patch
        or b"void TestMoveAction::testCallbacksSwap" not in raw_patch
    ):
        raise RuntimeError("#12595 PR/source scope evidence drifted")
    issues = {
        row.get("number"): row.get("issue")
        for row in manifest.get("issues", [])
        if isinstance(row, dict) and isinstance(row.get("issue"), dict)
    }
    issue_13486 = issues.get(13486)
    issue_14210 = issues.get(14210)
    if not isinstance(issue_13486, dict) or not isinstance(issue_14210, dict):
        raise RuntimeError("#12595 issue rows are missing")
    if (
        issue_13486.get("title")
        != "Incorrect sequence of calling callbacks `on_take` & `on_put` on swapping items in inventory"
        or "When swapping items in inventory" not in str(issue_13486.get("body"))
        or issue_14210.get("title")
        != "unexpected behavior related to stack size in register_on_player_inventory_action callback"
        or "both bottom entries are generated by the second shift-click" not in str(issue_14210.get("body"))
        or issue_14210.get("closed_at") != "2024-03-16T19:14:06Z"
    ):
        raise RuntimeError("#12595 distinct issue contracts drifted")
    return pull, issue_13486, issue_14210


def _relative(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise RuntimeError(f"#12595 {label} path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {".", ".."} for part in path.parts) or path.as_posix() != value:
        raise RuntimeError(f"#12595 {label} path is unsafe")
    return path.as_posix()


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("#12595 source manifest is not a safe regular file")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("#12595 source manifest is invalid")
    return value


def _read_bytes(path: Path) -> bytes:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("#12595 raw patch is not a safe regular file")
    return path.read_bytes()


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(root, artifact_id=reference.artifact_id, artifact_kind=reference.artifact_kind, semantic_version=reference.semantic_version)


def _assert_absent(root: Path) -> None:
    if (root / "artifacts" / Path(_OUTPUT.artifact_id) / _OUTPUT.semantic_version).exists():
        raise RuntimeError("#12595 authority re-audit already exists")


def _require_authorization(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "candidate-screening"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#12595 authority re-audit requires candidate-screening authorization")


def _now() -> str:
    return (datetime.now(UTC) + timedelta(microseconds=1)).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["write_luanti_12595_authority_reaudit"]
