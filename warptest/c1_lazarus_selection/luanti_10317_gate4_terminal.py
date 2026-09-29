"""Typed Gate 4 quality terminal for Luanti #10317.

Current-v2 source atomicity accepts the single bug issue #6065 and the raw
patch inventory is exact.  The only declared test patch is deletion-only: it
removes the ``Thread::kill`` test and supplies no executable regression
assertion.  The strict inverse therefore has no preserved fixed test contract
to compile, so API compatibility is mechanically clean but the test-manifest
Gate 4 contract is rejected.  No capsule, build, attempt, paid action, or
formal trial is started.
"""

from __future__ import annotations

import json
import os
import shlex
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


_CANDIDATE = {"repository_key": "luanti", "change_number": 10317}
_ENUMERATION = ArtifactReference(
    "repository_enumeration/luanti/current/v2", "repository_enumeration", "2.0"
)
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/10317/source/v2", "candidate_source_packet", "2.0"
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/10317/screening/v2", "candidate_test_patch", "2.0"
)
_CHECKPOINT = ArtifactReference(
    "screening_checkpoint/luanti/10317/current/v2", "screening_checkpoint", "2.0"
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/10317/preannotation/v2", "source_atomicity", "2.0"
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/10317/test_manifest/v2", "candidate_stage", "2.0"
)
_TERMINAL = ArtifactReference(
    "candidate_gate14_terminal/luanti/10317/preannotation/v1",
    "candidate_gate14_terminal",
    "1.0",
)
_PRODUCTION_PATHS = ("src/threading/thread.cpp", "src/threading/thread.h")
_TEST_PATH = "src/unittest/test_threading.cpp"
_RAW_PATHS = _PRODUCTION_PATHS + (_TEST_PATH,)


def audit_luanti_10317_gate4(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Reload current-v2 evidence and write a typed deletion-only terminal."""

    _require_authorization(authorization)
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
    _validate_chain(chain)
    source, patch, atomicity, manifest = chain[1], chain[2], chain[4], chain[5]
    source_manifest = _read_json(
        root / _safe_relative(source.payload["raw_evidence_path"])
    )
    raw_patch = (root / _safe_relative(patch.payload["raw_patch_path"])).read_bytes()
    sections = _parse_patch(raw_patch)
    patch_audit = _audit_patch(sections)
    if not patch_audit["inventory_exact"] or not patch_audit["test_deletion_only"]:
        raise RuntimeError("#10317 expected deletion-only Gate 4 evidence is absent")
    pull = source_manifest.get("pull")
    payload: dict[str, Any] = {
        "candidate": dict(_CANDIDATE),
        "state": "terminal",
        "outcome": "quality_rejected",
        "terminal": True,
        "resumable": False,
        "reason": "current_v2_test_manifest_rejected_deletion_only_no_executable_regression",
        "primary_issue_number": atomicity.payload["primary_issue_number"],
        "authoritative_issue_count": atomicity.payload["authoritative_issue_count"],
        "source_authority": {
            "pull_number": pull.get("number") if isinstance(pull, dict) else None,
            "pull_title": pull.get("title") if isinstance(pull, dict) else None,
            "merged": pull.get("merged") if isinstance(pull, dict) else None,
            "merge_revision": pull.get("merge_commit_sha")
            if isinstance(pull, dict)
            else None,
            "authoritative_issue_numbers": list(
                atomicity.payload["authoritative_issue_numbers"]
            ),
        },
        "current_v2_chain_revisions": {
            artifact.reference.artifact_id: artifact.created_at for artifact in chain
        },
        "patch_audit": patch_audit,
        "gate4_mechanical_audit": {
            "strict_inverse_scope": "passed_mechanical",
            "fixed_only_api": False,
            "api_compatibility": "passed_no_preserved_test_after_deletion",
            "test_manifest_state": manifest.payload["state"],
            "test_manifest_outcome": manifest.payload["outcome"],
            "test_manifest_reason": manifest.payload["details"]["reason"],
        },
        "current_gate_status": {
            "gate_1_source_authority": "passed",
            "gate_2_single_defect_mapping": "passed_authoritative_issue_6065",
            "gate_3_strict_inverse_patch": "passed_mechanical",
            "gate_4_fixed_test_buggy_api_compatibility": "passed_no_fixed_only_api",
            "gate_4_executable_test_contract": "failed_deletion_only",
            "gate_5_fixed_f2p_3x": "not_run_gate4_terminal",
            "gate_6_buggy_f2p_3x_failure": "not_run_gate4_terminal",
            "gate_7_fixed_buggy_p2p_3x": "not_run_gate4_terminal",
        },
        "fresh_gate57_epoch": {
            "required_attempt_count": 12,
            "attempt_count": 0,
            "not_run": True,
            "inherit_prior_attempts": False,
            "old_attempts_referenced": False,
        },
        "candidate_materialization_performed": False,
        "candidate_execution_performed": False,
        "new_build_started": False,
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "roster_modified": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "next_action": "do_not_schedule_capsule_build_or_gate57_execution_after_deletion_only_terminal",
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
            raise RuntimeError("#10317 terminal conflicts with current evidence")
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
        raise RuntimeError("#10317 terminal typed reload failed")
    return result


def _validate_chain(chain: tuple[StableArtifact, ...]) -> None:
    enumeration, source, patch, checkpoint, atomicity, manifest = chain
    details = manifest.payload.get("details")
    if (
        enumeration.payload.get("repository_key") != "luanti"
        or source.inputs != (_ENUMERATION,)
        or source.payload.get("candidate") != _CANDIDATE
        or source.payload.get("complete") is not True
        or patch.inputs != (_ENUMERATION, _SOURCE)
        or patch.payload.get("candidate") != _CANDIDATE
        or patch.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
        or patch.payload.get("source_manifest_path")
        != source.payload.get("raw_evidence_path")
        or tuple(patch.payload.get("production_paths", ())) != _PRODUCTION_PATHS
        or tuple(patch.payload.get("test_paths", ())) != (_TEST_PATH,)
        or checkpoint.inputs != (_ENUMERATION, _PATCH)
        or checkpoint.payload.get("candidate") != _CANDIDATE
        or checkpoint.payload.get("state") != "complete"
        or checkpoint.payload.get("outcome") != "patch_eligible"
        or checkpoint.payload.get("stage_artifact_id") != _PATCH.artifact_id
        or atomicity.inputs != (_ENUMERATION, _SOURCE, _PATCH, _CHECKPOINT)
        or atomicity.payload.get("candidate") != _CANDIDATE
        or atomicity.payload.get("complete") is not True
        or atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("authoritative_issue_numbers") != [6065]
        or atomicity.payload.get("primary_issue_number") != 6065
        or manifest.inputs != (_PATCH,)
        or manifest.payload.get("candidate") != _CANDIDATE
        or manifest.payload.get("complete") is not True
        or manifest.payload.get("stage") != "test_manifest"
        or manifest.payload.get("state") != "terminal"
        or manifest.payload.get("outcome") != "quality_rejected"
        or not isinstance(details, dict)
        or details.get("reason") != "deletion_only"
    ):
        raise RuntimeError("#10317 current-v2 Gate 1--4 chain drifted")


def _parse_patch(raw: bytes) -> dict[str, tuple[str, ...]]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in raw.decode("utf-8", errors="surrogateescape").splitlines(keepends=True):
        if line.startswith("diff --git "):
            parts = shlex.split(line.rstrip("\r\n"))
            if len(parts) != 4 or parts[:2] != ["diff", "--git"]:
                raise RuntimeError("#10317 raw patch header is invalid")
            left, right = parts[2], parts[3]
            if (
                not left.startswith("a/")
                or not right.startswith("b/")
                or left[2:] != right[2:]
            ):
                raise RuntimeError("#10317 raw patch path is invalid")
            current = _safe_relative(left[2:]).as_posix()
            if current in sections:
                raise RuntimeError("#10317 raw patch has duplicate path")
            sections[current] = []
        if current is not None:
            sections[current].append(line)
    return {path: tuple(lines) for path, lines in sections.items()}


def _audit_patch(sections: dict[str, tuple[str, ...]]) -> dict[str, Any]:
    raw_paths = tuple(sorted(sections))
    test_lines = sections[_TEST_PATH]
    additions = tuple(
        line
        for line in test_lines
        if line.startswith("+") and not line.startswith("+++")
    )
    removals = tuple(
        line
        for line in test_lines
        if line.startswith("-") and not line.startswith("---")
    )
    return {
        "inventory_exact": set(raw_paths) == set(_RAW_PATHS),
        "raw_patch_paths": list(raw_paths),
        "declared_production_paths": list(_PRODUCTION_PATHS),
        "declared_test_paths": [_TEST_PATH],
        "strict_inverse_scope": "production_only",
        "test_addition_count": len(additions),
        "test_removal_count": len(removals),
        "test_deletion_only": not additions and bool(removals),
        "removed_test_symbols": [
            line.strip()
            for line in removals
            if "kill" in line or "testThreadKill" in line
        ],
    }


def _safe_relative(value: object) -> PurePosixPath:
    if type(value) is not str or not value:
        raise RuntimeError("#10317 path is invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {".", ".."} for part in path.parts)
        or path.as_posix() != value
    ):
        raise RuntimeError("#10317 path is unsafe")
    return path


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("#10317 source manifest is missing or unsafe")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("candidate") != _CANDIDATE:
        raise RuntimeError("#10317 source manifest candidate binding drifted")
    return value


def _require_authorization(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#10317 Gate 4 terminal authorization is inactive")


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["audit_luanti_10317_gate4"]
