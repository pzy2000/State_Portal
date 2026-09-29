"""Typed Gate 2/4 blocker for Luanti change 6587.

The frozen current-v2 atomicity receipt has no authoritative issue because
the PR records ``Base issue: #6453`` rather than an explicit closing clause;
the captured links are #6453 and #9403.  A candidate-specific reclassification
would be required before Gate 2 admission.  Independently, the preserved new
test includes and instantiates ``ServerActiveObjectMap`` from two PR-added
production files, so a production-only inverse would leave the buggy tree
unable to compile that test.  This receipt is nonterminal/resumable and stops
before capsule materialization, build, execution, paid work, and formal trial.
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


_CANDIDATE = {"repository_key": "luanti", "change_number": 6587}
_ENUMERATION = ArtifactReference(
    "repository_enumeration/luanti/current/v2", "repository_enumeration", "2.0"
)
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/6587/source/v2", "candidate_source_packet", "2.0"
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/6587/screening/v2", "candidate_test_patch", "2.0"
)
_SCREENING = ArtifactReference(
    "screening_checkpoint/luanti/6587/current/v2", "screening_checkpoint", "2.0"
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/6587/preannotation/v2", "source_atomicity", "2.0"
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/6587/test_manifest/v2", "candidate_stage", "2.0"
)
_PREANNOTATION = ArtifactReference(
    "preannotation_checkpoint/luanti/6587/current/v2", "preannotation_checkpoint", "2.0"
)
_CHAIN = (_ENUMERATION, _SOURCE, _PATCH, _SCREENING, _ATOMICITY, _MANIFEST, _PREANNOTATION)
_BLOCKER = ArtifactReference(
    "candidate_gate57_6587_gate4_blocker/luanti/6587/native/v1",
    "candidate_gate57_6587_gate4_blocker",
    "1.0",
)
_PRODUCTION_PATHS = (
    "src/content_sao.cpp",
    "src/irr_aabb3d.h",
    "src/irr_v3d.h",
    "src/server/serveractiveobjectmap.cpp",
    "src/server/serveractiveobjectmap.h",
    "src/serverenvironment.cpp",
    "src/serverenvironment.h",
    "src/unittest/test.h",
)
_TEST_PATHS = ("src/unittest/test_serveractiveobjectmap.cpp",)
_SUPPORT_PATHS = (
    "build/android/jni/Android.mk",
    "src/server/CMakeLists.txt",
    "src/unittest/CMakeLists.txt",
    "util/travis/clang-format-whitelist.txt",
)
_RAW_PATHS = _SUPPORT_PATHS[:1] + _PRODUCTION_PATHS[:3] + _SUPPORT_PATHS[1:2] + _PRODUCTION_PATHS[3:7] + _SUPPORT_PATHS[2:3] + _PRODUCTION_PATHS[7:] + _TEST_PATHS + _SUPPORT_PATHS[3:]
_FIXED_API = "ServerActiveObjectMap"


def write_luanti_6587_gate57_blocker(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "infrastructure-audit"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#6587 blocker authorization is inactive")
    root = Path(artifact_root).absolute().resolve(strict=True)
    chain = tuple(_load(root, reference) for reference in _CHAIN)
    _validate_chain(chain)
    source, patch, _screening, atomicity, manifest, _checkpoint = chain[1:]
    source_manifest_path = _safe_payload_path(source.payload, "raw_evidence_path")
    raw_manifest = _read_json(root, source_manifest_path)
    patch_path = _safe_payload_path(patch.payload, "raw_patch_path")
    raw_patch = _read_bytes(root, patch_path)
    raw_paths, production_additions, test_references = _audit_patch(raw_patch)
    expected_paths = set(_PRODUCTION_PATHS + _TEST_PATHS + _SUPPORT_PATHS)
    if set(raw_paths) != expected_paths or len(raw_paths) != len(expected_paths):
        raise RuntimeError("#6587 raw patch inventory drifted")
    if not production_additions or not test_references:
        raise RuntimeError("#6587 expected fixed-only ServerActiveObjectMap API mismatch was not observed")
    required = {
        "candidate_profile": root / "artifacts/candidate_profile/luanti/6587",
        "fixed_source_capsule": root / "artifacts/fixed_source_capsule/luanti/6587",
        "gate57_profile": root / "artifacts/candidate_gate57_profile/luanti/6587",
        "native_regression_aggregate": root / "artifacts/native_regression_aggregate/luanti/6587",
        "native_regression_attempt": root / "artifacts/native_regression_attempt/luanti/6587",
    }
    present = {name: path.exists() for name, path in required.items()}
    if any(present.values()):
        raise RuntimeError("#6587 blocker inputs changed; re-audit exact current-v2 chain")
    issue_rows = {str(row["number"]): row["issue"] for row in raw_manifest["issues"]}
    pull = raw_manifest["pull"]
    payload: dict[str, Any] = {
        "candidate": dict(_CANDIDATE),
        "profile_id": "luanti-6587-entity-collision-v1",
        "state": "blocked",
        "outcome": "gate2_and_gate4_compatibility_blocked",
        "terminal": False,
        "resumable": True,
        "candidate_eligibility": "not_admitted_pending_base_issue_reclassification",
        "reason": "authoritative_issue_missing_and_preserved_test_uses_new_production_api",
        "candidate_execution_performed": False,
        "candidate_materialization_performed": False,
        "new_build_started": False,
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "does_not_freeze_or_eliminate_candidate": True,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "current_gate_status": {
            "gate_1_source_authority": "passed",
            "gate_2_single_defect_mapping": "blocked_authoritative_issue_missing",
            "gate_3_strict_inverse_patch": "blocked_before_gate_2_reclassification",
            "gate_4_fixed_test_api_compatibility": "blocked_fixed_only_api",
            "gate_5_fixed_f2p_3x": "not_run",
            "gate_6_buggy_f2p_3x_failure": "not_run",
            "gate_7_p2p_3x_both_pass": "not_run",
        },
        "gate2": {
            "atomicity_outcome": atomicity.payload["outcome"],
            "atomicity_reason": atomicity.payload["reason"],
            "authoritative_issue_numbers": list(atomicity.payload["authoritative_issue_numbers"]),
            "linked_issue_numbers": list(atomicity.payload["linked_issue_numbers"]),
            "base_issue_number": 6453,
            "base_issue_title": issue_rows["6453"].get("title"),
            "incidental_linked_issue_number": 9403,
            "incidental_issue_title": issue_rows["9403"].get("title"),
            "pull_body": pull.get("body"),
            "candidate_specific_reclassification_required": True,
        },
        "gate3": {
            "state": "blocked_before_gate_2_reclassification",
            "production_paths": list(_PRODUCTION_PATHS),
            "support_paths": list(_SUPPORT_PATHS),
            "test_paths": list(_TEST_PATHS),
            "raw_patch_paths": raw_paths,
            "production_only_inverse_scope": list(_PRODUCTION_PATHS),
            "test_hunk_preserved": True,
        },
        "gate4": {
            "state": "blocked_fixed_only_api",
            "fixed_api": _FIXED_API,
            "fixed_api_added_in_production_paths": production_additions,
            "preserved_test_api_reference_count": len(test_references),
            "preserved_test_api_reference_examples": test_references[:12],
            "explanation": (
                "The production-only inverse removes the PR-added serveractiveobjectmap.cpp/.h, "
                "while the preserved test includes and instantiates ServerActiveObjectMap."
            ),
        },
        "exact_closure": {name: {"registered": present[name]} for name in required},
        "fresh_gate57_epoch": {
            "required_attempt_count": 12,
            "attempt_count": 0,
            "candidate_specific_runner_created": False,
            "inherit_prior_attempts": False,
            "old_attempts_referenced": False,
        },
        "next_action": (
            "reclassify_unique_base_issue_6453_and_register_support_paths_then_reaudit_gate3_4"
        ),
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    inputs = _CHAIN
    existing = _try_load(root)
    if existing is not None:
        if existing.inputs != inputs or existing.payload != payload:
            raise RuntimeError("#6587 blocker conflicts with exact current-v2 state")
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
        raise RuntimeError("#6587 blocker typed reload failed")
    return artifact


def _validate_chain(chain: tuple[StableArtifact, ...]) -> None:
    if tuple(item.reference for item in chain) != _CHAIN:
        raise RuntimeError("#6587 current-v2 chain identity drifted")
    if any(item.payload.get("candidate") != _CANDIDATE for item in chain[1:]):
        raise RuntimeError("#6587 current-v2 candidate binding drifted")
    enumeration, source, patch, screening, atomicity, manifest, checkpoint = chain
    if enumeration.payload.get("repository_key") != "luanti" or enumeration.payload.get("complete") is not True:
        raise RuntimeError("#6587 enumeration is not complete Luanti evidence")
    if source.inputs != (_ENUMERATION,) or source.payload.get("complete") is not True:
        raise RuntimeError("#6587 source packet is not current-v2 bound")
    if (
        patch.inputs != (_ENUMERATION, _SOURCE)
        or patch.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
        or patch.payload.get("source_manifest_path") != source.payload.get("raw_evidence_path")
    ):
        raise RuntimeError("#6587 patch is not current-v2 eligible")
    if (
        screening.inputs != (_ENUMERATION, _PATCH)
        or screening.payload.get("state") != "complete"
        or screening.payload.get("outcome") != "patch_eligible"
    ):
        raise RuntimeError("#6587 screening checkpoint is not complete")
    if (
        atomicity.inputs != (_ENUMERATION, _SOURCE, _PATCH, _SCREENING)
        or atomicity.payload.get("complete") is not True
        or atomicity.payload.get("outcome") != "rejected"
        or atomicity.payload.get("reason") != "authoritative_issue_missing"
        or atomicity.payload.get("authoritative_issue_numbers") != []
        or atomicity.payload.get("linked_issue_numbers") != [6453, 9403]
    ):
        raise RuntimeError("#6587 source atomicity state changed")
    details = manifest.payload.get("details")
    if (
        manifest.inputs != (_PATCH,)
        or manifest.payload.get("complete") is not True
        or manifest.payload.get("stage") != "test_manifest"
        or manifest.payload.get("state") != "passed"
        or not isinstance(details, dict)
        or details.get("executable_test_paths") != list(_TEST_PATHS)
    ):
        raise RuntimeError("#6587 test manifest is not passed")
    if (
        checkpoint.inputs != (_MANIFEST,)
        or checkpoint.payload.get("state") != "active"
        or checkpoint.payload.get("current_artifact_id") != _MANIFEST.artifact_id
    ):
        raise RuntimeError("#6587 preannotation checkpoint is not current")


def _audit_patch(content: bytes) -> tuple[list[str], list[str], list[str]]:
    text = content.decode("utf-8", errors="surrogateescape")
    paths: list[str] = []
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines(keepends=True):
        match = re.fullmatch(r"diff --git a/(.+) b/(.+)\n?", line)
        if match:
            if match.group(1) != match.group(2):
                raise RuntimeError("#6587 raw patch path header is invalid")
            current = match.group(1)
            if current in sections:
                raise RuntimeError("#6587 raw patch duplicate section")
            paths.append(current)
            sections[current] = []
        if current is not None:
            sections[current].append(line)
    production = [line for path in _PRODUCTION_PATHS for line in sections[path]]
    test = sections[_TEST_PATHS[0]]
    additions = sorted(
        path for path in _PRODUCTION_PATHS
        if any(line.startswith("+") and _FIXED_API in line for line in sections[path])
    )
    references = [line[1:].strip() for line in test if line.startswith("+") and _FIXED_API in line]
    if not production or not additions or not references:
        raise RuntimeError("#6587 fixed-only API evidence is incomplete")
    return paths, additions, references


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(root, artifact_id=reference.artifact_id, artifact_kind=reference.artifact_kind, semantic_version=reference.semantic_version)


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
        raise RuntimeError(f"#6587 payload path {key} is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {".", ".."} for part in path.parts) or path.as_posix() != value:
        raise RuntimeError(f"#6587 payload path {key} is unsafe")
    return value


def _read_bytes(root: Path, relative: str) -> bytes:
    current = root
    for part in PurePosixPath(relative).parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError as error:
            raise RuntimeError("#6587 raw evidence file is missing") from error
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError("#6587 raw evidence path contains a symlink")
    if not stat.S_ISREG(current.lstat().st_mode):
        raise RuntimeError("#6587 raw evidence file is not regular")
    return current.read_bytes()


def _read_json(root: Path, relative: str) -> dict[str, Any]:
    try:
        value = json.loads(_read_bytes(root, relative).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("#6587 source manifest is invalid") from error
    if type(value) is not dict:
        raise RuntimeError("#6587 source manifest is not an object")
    return value


def _now() -> str:
    return (datetime.now(UTC) + timedelta(microseconds=1)).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["write_luanti_6587_gate57_blocker"]
