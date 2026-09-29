"""Typed nonterminal blocker for the current-v2 Luanti #10085 candidate.

The persisted atomicity receipt rejects this candidate because the PR
authoritatively fixes two distinct issues (#5878 and #9933).  The raw diff
also contains three configuration/documentation sections not represented by
the current typed production/test inventory.  This receipt therefore stops
before Gate 3/4 admission and before any source capsule, build, or fresh
Gate 5--7 attempt.  It does not eliminate the candidate or inherit an old
execution.
"""

from __future__ import annotations

import json
import os
import re
import stat
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


_CANDIDATE = {"repository_key": "luanti", "change_number": 10085}
_ENUMERATION = ArtifactReference(
    "repository_enumeration/luanti/current/v2", "repository_enumeration", "2.0"
)
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/10085/source/v2", "candidate_source_packet", "2.0"
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/10085/screening/v2", "candidate_test_patch", "2.0"
)
_SCREENING = ArtifactReference(
    "screening_checkpoint/luanti/10085/current/v2", "screening_checkpoint", "2.0"
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/10085/preannotation/v2", "source_atomicity", "2.0"
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/10085/test_manifest/v2", "candidate_stage", "2.0"
)
_PREANNOTATION = ArtifactReference(
    "preannotation_checkpoint/luanti/10085/current/v2", "preannotation_checkpoint", "2.0"
)
_CHAIN = (_ENUMERATION, _SOURCE, _PATCH, _SCREENING, _ATOMICITY, _MANIFEST, _PREANNOTATION)
_BLOCKER = ArtifactReference(
    "candidate_gate57_10085_current_v2_blocker/luanti/10085/native/v1",
    "candidate_gate57_10085_current_v2_blocker",
    "1.0",
)
_PRODUCTION_PATHS = (
    "builtin/mainmenu/common.lua",
    "builtin/mainmenu/init.lua",
    "builtin/mainmenu/serverlistmgr.lua",
    "builtin/mainmenu/tab_online.lua",
    "src/client/clientlauncher.cpp",
    "src/defaultsettings.cpp",
    "src/script/lua_api/l_mainmenu.cpp",
    "src/script/lua_api/l_mainmenu.h",
    "src/script/lua_api/l_util.cpp",
    "src/script/lua_api/l_util.h",
    "src/serverlist.cpp",
    "src/serverlist.h",
)
_TEST_PATHS = (
    "builtin/mainmenu/tests/favorites_wellformed.txt",
    "builtin/mainmenu/tests/serverlistmgr_spec.lua",
)
_EXPECTED_RAW = _PRODUCTION_PATHS + _TEST_PATHS + (
    "builtin/settingtypes.txt",
    "doc/menu_lua_api.txt",
    "minetest.conf.example",
)


def write_luanti_10085_current_v2_blocker(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "infrastructure-audit"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#10085 current-v2 blocker authorization is inactive")
    root = Path(artifact_root).absolute().resolve(strict=True)
    chain = tuple(_load(root, reference) for reference in _CHAIN)
    _validate_chain(chain)
    source, patch, _screening, atomicity, manifest, _checkpoint = (
        chain[1], chain[2], chain[3], chain[4], chain[5], chain[6]
    )
    source_manifest_path = _safe_payload_path(source.payload, "raw_evidence_path")
    raw_manifest = _read_json(root, source_manifest_path)
    patch_path = _safe_payload_path(patch.payload, "raw_patch_path")
    raw_paths = _patch_paths(_read_bytes(root, patch_path))
    undeclared = sorted(set(raw_paths) - set(_PRODUCTION_PATHS + _TEST_PATHS))
    missing = sorted(set(_PRODUCTION_PATHS + _TEST_PATHS) - set(raw_paths))
    if len(raw_paths) != len(_EXPECTED_RAW) or set(raw_paths) != set(_EXPECTED_RAW):
        raise RuntimeError("#10085 raw patch inventory drifted from current-v2 evidence")
    issue_values = {
        str(number): next(item["issue"] for item in raw_manifest["issues"] if item["number"] == number)
        for number in raw_manifest["issue_numbers"]
    }
    pull = raw_manifest["pull"]
    required = {
        "candidate_profile": root / "artifacts/candidate_profile/luanti/10085",
        "fixed_source_capsule": root / "artifacts/fixed_source_capsule/luanti/10085",
        "gate57_profile": root / "artifacts/candidate_gate57_profile/luanti/10085",
        "native_regression_aggregate": root / "artifacts/native_regression_aggregate/luanti/10085",
        "native_regression_attempt": root / "artifacts/native_regression_attempt/luanti/10085",
    }
    present = {name: path.exists() for name, path in required.items()}
    if any(present.values()):
        raise RuntimeError("#10085 blocker inputs changed; re-audit exact current-v2 chain")
    payload: dict[str, Any] = {
        "candidate": dict(_CANDIDATE),
        "profile_id": "luanti-10085-favorites-json-server-list-v1",
        "state": "blocked",
        "outcome": "infrastructure_blocked",
        "terminal": False,
        "resumable": True,
        "candidate_eligibility": "not_admitted_pending_single_defect_mapping",
        "reason": "current_v2_source_atomicity_rejected_multiple_authoritative_issues",
        "candidate_execution_performed": False,
        "candidate_materialization_performed": False,
        "new_build_started": False,
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "does_not_freeze_or_eliminate_candidate": True,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "downstream_8_13_authorized": False,
        "current_gate_status": {
            "gate_1_source_authority": "passed",
            "gate_2_single_defect_mapping": "blocked_multiple_authoritative_issues",
            "gate_3_strict_inverse_patch": "not_admissible_before_gate_2_repair",
            "gate_4_fixed_test_api_compatibility": "not_admissible_before_gate_2_repair",
            "gate_5_fixed_f2p_3x": "not_run",
            "gate_6_buggy_f2p_3x_failure": "not_run",
            "gate_7_p2p_3x_both_pass": "not_run",
        },
        "source_atomicity": {
            "receipt_outcome": atomicity.payload["outcome"],
            "reason": atomicity.payload["reason"],
            "authoritative_issue_numbers": list(atomicity.payload["authoritative_issue_numbers"]),
            "linked_issue_numbers": list(atomicity.payload["linked_issue_numbers"]),
            "single_defect_mapping_admissible": False,
            "primary_issue_number": atomicity.payload.get("primary_issue_number"),
        },
        "authoritative_source_audit": {
            "pull_number": pull.get("number"),
            "pull_title": pull.get("title"),
            "pull_merged": pull.get("merged"),
            "pull_body_fix_claim": "Fixes #5878  Fixes #9933",
            "issue_titles": {number: value.get("title") for number, value in issue_values.items()},
        },
        "source_patch_binding": {
            "source_manifest_path": source_manifest_path,
            "raw_patch_path": patch_path,
            "production_paths": list(_PRODUCTION_PATHS),
            "test_paths": list(_TEST_PATHS),
            "raw_patch_paths": raw_paths,
            "undeclared_support_like_paths": undeclared,
            "missing_declared_paths": missing,
            "section_count": len(raw_paths),
            "strict_inverse_patch": "not_admissible_before_gate_2_repair",
            "test_hunk_preserved": True,
        },
        "exact_closure": {
            "fixed_source_capsule_present": False,
            "candidate_profile_present": False,
            "gate57_profile_present": False,
            "native_regression_aggregate_present": False,
            "native_regression_attempt_present": False,
            "closure_admissible": False,
        },
        "fresh_gate57_epoch": {
            "required_attempt_count": 12,
            "attempt_count": 0,
            "candidate_specific_runner_created": False,
            "inherit_prior_attempts": False,
            "old_attempts_referenced": False,
        },
        "next_action": (
            "repair_current_v2_single_authority_mapping_and_register_support_paths_then_reaudit_gate3_4_before_gate57"
        ),
        "resource_policy": {
            "network": False,
            "paid": False,
            "formal_trials": False,
            "downstream_8_13": False,
        },
    }
    inputs = _CHAIN
    existing = _try_load(root)
    if existing is not None:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError("#10085 blocker conflicts with exact current-v2 state")
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
    if artifact.inputs != inputs or artifact.payload != payload:
        raise RuntimeError("#10085 blocker typed reload failed")
    return artifact


def _validate_chain(chain: tuple[StableArtifact, ...]) -> None:
    if tuple(item.reference for item in chain) != _CHAIN:
        raise RuntimeError("#10085 current-v2 chain identity drifted")
    if any(item.payload.get("candidate") != _CANDIDATE for item in chain[1:]):
        raise RuntimeError("#10085 current-v2 candidate binding drifted")
    enumeration, source, patch, screening, atomicity, manifest, checkpoint = chain
    if enumeration.payload.get("repository_key") != "luanti" or enumeration.payload.get("complete") is not True:
        raise RuntimeError("#10085 enumeration is not complete Luanti evidence")
    if source.inputs != (_ENUMERATION,) or source.payload.get("complete") is not True:
        raise RuntimeError("#10085 source packet is not current-v2 bound")
    if (
        patch.inputs != (_ENUMERATION, _SOURCE)
        or patch.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
        or patch.payload.get("source_manifest_path") != source.payload.get("raw_evidence_path")
    ):
        raise RuntimeError("#10085 patch is not current-v2 eligible")
    if (
        screening.inputs != (_ENUMERATION, _PATCH)
        or screening.payload.get("state") != "complete"
        or screening.payload.get("outcome") != "patch_eligible"
    ):
        raise RuntimeError("#10085 screening checkpoint is not complete")
    if (
        atomicity.inputs != (_ENUMERATION, _SOURCE, _PATCH, _SCREENING)
        or atomicity.payload.get("complete") is not True
        or atomicity.payload.get("outcome") != "rejected"
        or atomicity.payload.get("reason") != "multiple_authoritative_issues"
        or atomicity.payload.get("authoritative_issue_numbers") != [5878, 9933]
        or atomicity.payload.get("linked_issue_numbers") != [5878, 9168, 9933, 10766]
    ):
        raise RuntimeError("#10085 source atomicity state changed")
    details = manifest.payload.get("details")
    if (
        manifest.inputs != (_PATCH,)
        or manifest.payload.get("complete") is not True
        or manifest.payload.get("stage") != "test_manifest"
        or manifest.payload.get("state") != "passed"
        or not isinstance(details, dict)
        or details.get("executable_test_paths") != ["builtin/mainmenu/tests/serverlistmgr_spec.lua"]
    ):
        raise RuntimeError("#10085 test manifest is not passed")
    if (
        checkpoint.inputs != (_MANIFEST,)
        or checkpoint.payload.get("state") != "active"
        or checkpoint.payload.get("current_artifact_id") != _MANIFEST.artifact_id
    ):
        raise RuntimeError("#10085 preannotation checkpoint is not current")


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _try_load(root: Path) -> StableArtifact | None:
    try:
        return _load(root, _BLOCKER)
    except RuntimeError as error:
        if str(error) in {"artifact directory is missing", "artifact current head is missing"}:
            return None
        raise


def _safe_payload_path(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if type(value) is not str or not value:
        raise RuntimeError(f"#10085 payload path {key} is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {".", ".."} for part in path.parts) or path.as_posix() != value:
        raise RuntimeError(f"#10085 payload path {key} is unsafe")
    return value


def _safe_raw_path(root: Path, relative: str) -> Path:
    current = root
    for part in PurePosixPath(relative).parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError as error:
            raise RuntimeError("#10085 raw evidence file is missing") from error
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError("#10085 raw evidence path contains a symlink")
    return current


def _read_bytes(root: Path, relative: str) -> bytes:
    path = _safe_raw_path(root, relative)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError("#10085 raw evidence path is not a regular file")
    return path.read_bytes()


def _read_json(root: Path, relative: str) -> dict[str, Any]:
    try:
        value = json.loads(_read_bytes(root, relative).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("#10085 source manifest is invalid") from error
    if type(value) is not dict:
        raise RuntimeError("#10085 source manifest is not an object")
    return value


def _patch_paths(content: bytes) -> list[str]:
    paths: list[str] = []
    for line in content.decode("utf-8", errors="surrogateescape").splitlines():
        match = re.fullmatch(r"diff --git a/(.+) b/(.+)", line)
        if match:
            if match.group(1) != match.group(2):
                raise RuntimeError("#10085 raw patch path header is invalid")
            paths.append(match.group(1))
    if not paths or len(paths) != len(set(paths)):
        raise RuntimeError("#10085 raw patch has no unique sections")
    return paths


def _now() -> str:
    return (datetime.now(UTC) + timedelta(microseconds=1)).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["write_luanti_10085_current_v2_blocker"]
