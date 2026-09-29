"""Typed current-v2 Gate 2 blocker for Luanti #17024.

The exact current-v2 source, patch, checkpoint, and test-manifest heads are
complete, but source atomicity rejects the candidate because the captured
manifest has no authoritative issue (only the linked issue #8196).  This
receipt is deliberately nonterminal: it records the missing authority mapping
and stops before capsule creation, native builds, attempts, paid work, or
formal trials.  No historical execution artifact is loaded or inherited.
"""

from __future__ import annotations

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


_CANDIDATE = {"repository_key": "luanti", "change_number": 17024}
_ENUMERATION = ArtifactReference(
    "repository_enumeration/luanti/current/v2", "repository_enumeration", "2.0"
)
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/17024/source/v2", "candidate_source_packet", "2.0"
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/17024/screening/v2", "candidate_test_patch", "2.0"
)
_CHECKPOINT = ArtifactReference(
    "screening_checkpoint/luanti/17024/current/v2", "screening_checkpoint", "2.0"
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/17024/preannotation/v2", "source_atomicity", "2.0"
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/17024/test_manifest/v2", "candidate_stage", "2.0"
)
_BLOCKER = ArtifactReference(
    "candidate_gate57_17024_current_v2_blocker/luanti/17024/native/v1",
    "candidate_gate57_17024_current_v2_blocker",
    "1.0",
)

_PRODUCTION_PATHS = (
    "src/inventorymanager.cpp",
    "src/script/cpp_api/s_item.cpp",
    "src/script/cpp_api/s_item.h",
)
_TEST_PATHS = (
    "builtin/game/tests/test_moveaction.lua",
    "src/unittest/test_moveaction.cpp",
)
_SUPPORT_PATHS = ("doc/breakages.md", "doc/lua_api.md")


def audit_luanti_17024_gate2(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Audit exact current-v2 Gate 1--4 heads and persist a typed blocker."""

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
    enumeration, source, patch, checkpoint, atomicity, manifest = chain
    raw_patch_path = _safe_relative(patch.payload["raw_patch_path"])
    sections = _parse_patch((root / raw_patch_path).read_bytes())
    patch_audit = _audit_patch(sections, patch.payload)
    if not patch_audit["inventory_exact"]:
        raise RuntimeError("#17024 current-v2 patch inventory is not exact")

    source_manifest_path = _safe_relative(source.payload["raw_evidence_path"])
    manifest_payload = {
        "candidate": _CANDIDATE,
        "pull_number": _manifest_pull_number(root / source_manifest_path),
        "pull_title": _manifest_pull_title(root / source_manifest_path),
        "merged": _manifest_merged(root / source_manifest_path),
        "merge_revision": _manifest_merge_commit(root / source_manifest_path),
        "linked_issue_numbers": list(atomicity.payload["linked_issue_numbers"]),
        "authoritative_issue_numbers": list(
            atomicity.payload["authoritative_issue_numbers"]
        ),
    }
    payload: dict[str, Any] = {
        "candidate": dict(_CANDIDATE),
        "profile_id": "luanti-17024-on-drop-semantics-v1",
        "state": "blocked",
        "outcome": "gate2_authoritative_issue_missing",
        "terminal": False,
        "resumable": True,
        "candidate_eligibility": "not_admitted_pending_authority_mapping",
        "does_not_freeze_or_eliminate_candidate": True,
        "reason": "current_v2_source_atomicity_rejected_authoritative_issue_missing",
        "current_gate_status": {
            "gate_1_source_authority": "passed",
            "gate_2_single_defect_mapping": "blocked_authoritative_issue_missing",
            "gate_3_strict_inverse_patch": "not_admitted_before_gate2_repair",
            "gate_4_fixed_test_api_compatibility": "not_admitted_before_gate2_repair",
            "gate_5_fixed_f2p_3x": "not_run_gate2_blocker",
            "gate_6_buggy_f2p_3x_failure": "not_run_gate2_blocker",
            "gate_7_fixed_buggy_p2p_3x": "not_run_gate2_blocker",
        },
        "chain_refs": {
            "repository_enumeration": enumeration.reference.artifact_id,
            "source": source.reference.artifact_id,
            "patch": patch.reference.artifact_id,
            "screening_checkpoint": checkpoint.reference.artifact_id,
            "atomicity": atomicity.reference.artifact_id,
            "test_manifest": manifest.reference.artifact_id,
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
        "patch_audit": patch_audit,
        "source_manifest_audit": manifest_payload,
        "api_compatibility_audit": {
            "fixed_only_api_observed": False,
            "preserved_test_paths": list(_TEST_PATHS),
            "changed_production_api": [
                "ScriptApiItem::item_OnDrop(ItemStack&) -> u16 item_OnDrop(const ItemStack&)"
            ],
            "status": "not_admitted_before_gate2_repair",
            "note": (
                "The preserved tests exercise Drop actions and callback patterns; they do "
                "not directly call the changed item_OnDrop C++ API. Gate 4 remains "
                "unadmitted until the Gate 2 authority mapping is repaired."
            ),
        },
        "fresh_gate57_epoch": {
            "required_attempt_count": 12,
            "attempt_count": 0,
            "inherit_prior_attempts": False,
            "old_attempts_referenced": False,
            "candidate_specific_runner_created": False,
        },
        "candidate_materialization_performed": False,
        "candidate_execution_performed": False,
        "new_build_started": False,
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "roster_modified": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "downstream_8_13_authorized": False,
        "next_action": (
            "repair_or_recapture the authoritative issue mapping in current-v2, "
            "then re-audit Gates 2--4 before registering any capsule or fresh runner"
        ),
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    inputs = tuple(item.reference for item in chain)
    try:
        existing = _load(root, _BLOCKER)
    except RuntimeError as error:
        if str(error) not in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            raise
    else:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError("#17024 current-v2 blocker conflicts with exact state")
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
        raise RuntimeError("#17024 current-v2 blocker typed reload failed")
    return artifact


def _validate_chain(chain: tuple[StableArtifact, ...]) -> None:
    enumeration, source, patch, checkpoint, atomicity, manifest = chain
    if enumeration.payload.get("repository_key") != "luanti":
        raise RuntimeError("#17024 enumeration is not Luanti")
    if (
        source.inputs != (_ENUMERATION,)
        or source.payload.get("candidate") != _CANDIDATE
        or source.payload.get("complete") is not True
    ):
        raise RuntimeError("#17024 source chain binding drifted")
    if (
        patch.inputs != (_ENUMERATION, _SOURCE)
        or patch.payload.get("candidate") != _CANDIDATE
        or patch.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
        or patch.payload.get("source_manifest_path")
        != source.payload.get("raw_evidence_path")
        or tuple(patch.payload.get("production_paths", ())) != _PRODUCTION_PATHS
        or tuple(patch.payload.get("test_paths", ())) != _TEST_PATHS
    ):
        raise RuntimeError("#17024 patch chain binding drifted")
    if (
        checkpoint.inputs != (_ENUMERATION, _PATCH)
        or checkpoint.payload.get("candidate") != _CANDIDATE
        or checkpoint.payload.get("state") != "complete"
        or checkpoint.payload.get("outcome") != "patch_eligible"
        or checkpoint.payload.get("stage_artifact_id") != _PATCH.artifact_id
    ):
        raise RuntimeError("#17024 screening checkpoint binding drifted")
    if (
        atomicity.inputs != (_ENUMERATION, _SOURCE, _PATCH, _CHECKPOINT)
        or atomicity.payload.get("candidate") != _CANDIDATE
        or atomicity.payload.get("complete") is not True
        or atomicity.payload.get("outcome") != "rejected"
        or atomicity.payload.get("reason") != "authoritative_issue_missing"
        or atomicity.payload.get("authoritative_issue_numbers") != []
        or atomicity.payload.get("linked_issue_numbers") != [8196]
    ):
        raise RuntimeError("#17024 source atomicity state drifted")
    details = manifest.payload.get("details")
    if (
        manifest.inputs != (_PATCH,)
        or manifest.payload.get("candidate") != _CANDIDATE
        or manifest.payload.get("complete") is not True
        or manifest.payload.get("stage") != "test_manifest"
        or manifest.payload.get("state") != "passed"
        or not isinstance(details, dict)
        or details.get("test_count") != 2
        or details.get("executable_test_paths") != list(_TEST_PATHS)
    ):
        raise RuntimeError("#17024 test manifest chain binding drifted")


def _parse_patch(raw: bytes) -> dict[str, tuple[str, ...]]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in raw.decode("utf-8", errors="surrogateescape").splitlines(keepends=True):
        if line.startswith("diff --git "):
            parts = shlex.split(line.rstrip("\r\n"))
            if len(parts) != 4 or parts[:2] != ["diff", "--git"]:
                raise RuntimeError("#17024 raw patch header is invalid")
            left, right = parts[2], parts[3]
            if (
                not left.startswith("a/")
                or not right.startswith("b/")
                or left[2:] != right[2:]
            ):
                raise RuntimeError("#17024 raw patch path is invalid")
            current = _safe_relative(left[2:]).as_posix()
            if current in sections:
                raise RuntimeError("#17024 raw patch has duplicate path")
            sections[current] = []
        if current is not None:
            sections[current].append(line)
    if not sections:
        raise RuntimeError("#17024 raw patch is empty")
    return {path: tuple(lines) for path, lines in sections.items()}


def _audit_patch(
    sections: dict[str, tuple[str, ...]], patch_payload: dict[str, Any]
) -> dict[str, Any]:
    raw_paths = tuple(sorted(sections))
    expected = set(_PRODUCTION_PATHS + _TEST_PATHS + _SUPPORT_PATHS)
    inventory_exact = set(raw_paths) == expected
    added_test = "\n".join(line for path in _TEST_PATHS for line in sections[path])
    added_production = "\n".join(
        line for path in _PRODUCTION_PATHS for line in sections[path]
    )
    markers = {
        "drop_test": all(
            marker in added_test
            for marker in ("testDrop", "testDropOne", "testDropUnallowed")
        ),
        "on_drop_callback": "on_drop" in added_test,
        "drop_count_api": "actually_dropped_count" in added_production,
        "item_on_drop_return_count": "u16 item_OnDrop" in added_production,
    }
    return {
        "inventory_exact": inventory_exact,
        "raw_patch_paths": list(raw_paths),
        "declared_production_paths": list(patch_payload["production_paths"]),
        "declared_test_paths": list(patch_payload["test_paths"]),
        "support_paths_excluded_from_inverse": list(_SUPPORT_PATHS),
        "undeclared_paths": sorted(
            set(raw_paths) - set(_PRODUCTION_PATHS + _TEST_PATHS + _SUPPORT_PATHS)
        ),
        "strict_inverse_scope": "production_only_with_support_paths_excluded",
        "markers": markers,
        "static_partition": "passed"
        if inventory_exact and all(markers.values())
        else "failed",
    }


def _safe_relative(value: object) -> PurePosixPath:
    if type(value) is not str or not value:
        raise RuntimeError("#17024 path is invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {".", ".."} for part in path.parts)
        or path.as_posix() != value
    ):
        raise RuntimeError("#17024 path is unsafe")
    return path


def _manifest_json(path: Path) -> dict[str, Any]:
    import json

    if not path.is_file() or path.is_symlink():
        raise RuntimeError("#17024 source manifest is missing or unsafe")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("candidate") != _CANDIDATE:
        raise RuntimeError("#17024 source manifest candidate binding drifted")
    return value


def _manifest_pull(path: Path) -> dict[str, Any]:
    pull = _manifest_json(path).get("pull")
    if not isinstance(pull, dict):
        raise RuntimeError("#17024 source manifest pull is invalid")
    return pull


def _manifest_pull_number(path: Path) -> int:
    value = _manifest_pull(path).get("number")
    if type(value) is not int:
        raise RuntimeError("#17024 pull number is invalid")
    return value


def _manifest_pull_title(path: Path) -> str:
    value = _manifest_pull(path).get("title")
    if type(value) is not str:
        raise RuntimeError("#17024 pull title is invalid")
    return value


def _manifest_merged(path: Path) -> bool:
    value = _manifest_pull(path).get("merged")
    if type(value) is not bool:
        raise RuntimeError("#17024 pull merge state is invalid")
    return value


def _manifest_merge_commit(path: Path) -> str | None:
    value = _manifest_pull(path).get("merge_commit_sha")
    if value is not None and type(value) is not str:
        raise RuntimeError("#17024 pull merge commit is invalid")
    return value


def _require_authorization(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#17024 Gate 2 audit authorization is inactive")


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["audit_luanti_17024_gate2"]
