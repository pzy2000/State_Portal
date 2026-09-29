"""Strict Gate 4 terminal proof for Luanti #17187.

The frozen translation unit test added by the PR calls two helpers introduced
in the same PR.  A production-only inverse removes those helpers while
preserving the test, so the fixed test cannot compile against the buggy API.
This is intentionally static: no capsule, native build, old attempt, paid
operation, or formal trial is allowed after a hard Gate 4 failure.
"""

from __future__ import annotations

import os
import shlex
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


_CANDIDATE = {"repository_key": "luanti", "change_number": 17187}
_SOURCE = ArtifactReference("candidate_source_packet/luanti/17187/source/v2", "candidate_source_packet", "2.0")
_PATCH = ArtifactReference("candidate_test_patch/luanti/17187/screening/v2", "candidate_test_patch", "2.0")
_ATOMICITY = ArtifactReference("source_atomicity/luanti/17187/preannotation/v2", "source_atomicity", "2.0")
_MANIFEST = ArtifactReference("candidate_stage/luanti/17187/test_manifest/v2", "candidate_stage", "2.0")
_TERMINAL = ArtifactReference("candidate_gate14_terminal/luanti/17187/preannotation/v1", "candidate_gate14_terminal", "1.0")
_PRODUCTION = ("src/server.cpp", "src/translation.cpp", "src/translation.h")
_TEST = "src/test/test_translations.cpp"


def audit_luanti_17187_gate4(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Reload current-v2 evidence and write an idempotent Gate 4 terminal."""

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#17187 Gate 4 requires active preannotation execution")
    root = Path(artifact_root).absolute().resolve(strict=True)
    source, patch, atomicity, manifest = tuple(
        _load(root, reference) for reference in (_SOURCE, _PATCH, _ATOMICITY, _MANIFEST)
    )
    _validate_chain(source, patch, atomicity, manifest)
    raw_patch = _regular_file(root / _safe_relative(patch.payload["raw_patch_path"])).read_bytes()
    audit = _audit_patch(raw_patch)
    if not audit["incompatible"]:
        raise RuntimeError("#17187 expected fixed-only translation API evidence is absent")

    payload: dict[str, Any] = {
        "candidate": _CANDIDATE,
        "profile_id": "luanti-17187-translation-filenames-v1",
        "state": "terminal",
        "outcome": "gate4_incompatible",
        "terminal": True,
        "resumable": False,
        "candidate_execution_performed": False,
        "candidate_materialization_performed": False,
        "new_build_started": False,
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "reason": "preserved_fixed_test_calls_pr_introduced_translation_helpers_absent_after_production_inverse",
        "current_gate_status": {
            "source_authority": "passed",
            "single_defect_mapping": "passed",
            "strict_inverse_patch": "passed",
            "test_api_compatibility": "failed_fixed_only_api",
            "fresh_gate57_epoch": "not_run_terminal",
        },
        "current_v2_chain_revisions": {
            artifact.reference.artifact_id: artifact.created_at
            for artifact in (source, patch, atomicity, manifest)
        },
        "patch_audit": audit,
        "fresh_epoch": {
            "required_attempt_count": 12,
            "attempt_count": 0,
            "not_run": True,
            "inherit_prior_attempts": False,
            "old_attempts_referenced": False,
        },
        "next_action": "do_not_schedule_capsule_build_or_gate57_execution; remove_candidate_from_gate57_pool",
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    inputs = (_SOURCE, _PATCH, _ATOMICITY, _MANIFEST)
    try:
        existing = _load(root, _TERMINAL)
    except RuntimeError as error:
        if str(error) not in {"artifact directory is missing", "artifact current head is missing"}:
            raise
    else:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError("#17187 Gate 4 terminal conflicts with current evidence")
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
        raise RuntimeError("#17187 Gate 4 terminal reload failed")
    return result


def _validate_chain(
    source: StableArtifact, patch: StableArtifact, atomicity: StableArtifact, manifest: StableArtifact
) -> None:
    if any(artifact.payload.get("candidate") != _CANDIDATE for artifact in (source, patch, atomicity, manifest)):
        raise RuntimeError("#17187 current-v2 candidate binding is invalid")
    if source.payload.get("complete") is not True:
        raise RuntimeError("#17187 source closure is incomplete")
    if (
        patch.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
        or tuple(patch.payload.get("production_paths", ())) != _PRODUCTION
        or tuple(patch.payload.get("test_paths", ())) != (_TEST,)
        or patch.payload.get("source_manifest_path") != source.payload.get("raw_evidence_path")
    ):
        raise RuntimeError("#17187 patch inventory drifted")
    if (
        atomicity.payload.get("complete") is not True
        or atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("authoritative_issue_count") != 1
        or atomicity.payload.get("primary_issue_number") != 17026
    ):
        raise RuntimeError("#17187 source atomicity drifted")
    details = manifest.payload.get("details")
    if (
        manifest.payload.get("complete") is not True
        or manifest.payload.get("stage") != "test_manifest"
        or manifest.payload.get("state") != "passed"
        or not isinstance(details, dict)
        or details.get("executable_test_paths") != [_TEST]
        or details.get("test_count") != 1
    ):
        raise RuntimeError("#17187 test manifest drifted")


def _audit_patch(raw_patch: bytes) -> dict[str, object]:
    sections = _parse_patch(raw_patch)
    if set(sections) != set(_PRODUCTION + (_TEST,)):
        raise RuntimeError("#17187 raw patch paths are not strictly accounted for")
    production = tuple(line for path in _PRODUCTION for line in sections[path])
    test = sections[_TEST]
    introduced = {
        "getFileBaseName": any(
            line.startswith("+") and "getFileBaseName(std::string_view filename)" in line
            for line in production
        ),
        "isTranslationFileType": any(
            line.startswith("+") and "isTranslationFileType(std::string_view filename)" in line
            for line in production
        ),
    }
    fixed_test_calls = [
        line[1:].strip()
        for line in test
        if line.startswith("+")
        and ("Translations::getFileBaseName(" in line or "Translations::isTranslationFileType(" in line)
    ]
    incompatible = all(introduced.values()) and bool(fixed_test_calls)
    return {
        "declared_production_paths": list(_PRODUCTION),
        "declared_test_paths": [_TEST],
        "raw_patch_paths": sorted(sections),
        "inverse_scope_strict": True,
        "changed_api": introduced,
        "fixed_test_calls": fixed_test_calls,
        "fixed_only_api": incompatible,
        "buggy_compile_expected": False,
        "explanation": (
            "The production-only inverse removes getFileBaseName and isTranslationFileType, "
            "but the preserved test hunk directly calls both helpers."
        ),
        "incompatible": incompatible,
    }


def _parse_patch(raw_patch: bytes) -> dict[str, tuple[str, ...]]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in raw_patch.decode("utf-8", errors="surrogateescape").splitlines(keepends=True):
        if line.startswith("diff --git "):
            parts = shlex.split(line.rstrip("\r\n"))
            if len(parts) != 4 or parts[:2] != ["diff", "--git"]:
                raise RuntimeError("#17187 raw patch header is invalid")
            left, right = parts[2], parts[3]
            if not left.startswith("a/") or not right.startswith("b/") or left[2:] != right[2:]:
                raise RuntimeError("#17187 raw patch path is invalid")
            current = _safe_relative(left[2:])
            if current in sections:
                raise RuntimeError("#17187 raw patch has duplicate path")
            sections[current] = []
        if current is not None:
            sections[current].append(line)
    if not sections:
        raise RuntimeError("#17187 raw patch is empty")
    return {path: tuple(lines) for path, lines in sections.items()}


def _safe_relative(value: object) -> str:
    if type(value) is not str or not value:
        raise RuntimeError("#17187 path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {".", ".."} for part in path.parts) or path.as_posix() != value:
        raise RuntimeError("#17187 path is unsafe")
    return path.as_posix()


def _regular_file(path: Path) -> Path:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("#17187 raw patch is missing or unsafe")
    return path


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(root, artifact_id=reference.artifact_id, artifact_kind=reference.artifact_kind, semantic_version=reference.semantic_version)


def _now() -> str:
    return (datetime.now(UTC) + timedelta(microseconds=1)).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["audit_luanti_17187_gate4"]
