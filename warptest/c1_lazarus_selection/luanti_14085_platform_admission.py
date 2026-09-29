"""Current-v2 platform-admission blocker for Luanti #14085.

Gate 1--4 are statically admissible, but the regression discriminator lives
exclusively under ``_WIN32``.  A Darwin/POSIX build cannot provide the required
buggy F2P failure, and no candidate-specific Windows runner/toolchain closure
is registered.  This module records that infrastructure blocker without
materialization, build, execution, or reuse of attempts.
"""

from __future__ import annotations

import json
import os
import platform
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


_CANDIDATE = {"repository_key": "luanti", "change_number": 14085}
_ENUMERATION = ArtifactReference(
    "repository_enumeration/luanti/current/v2", "repository_enumeration", "2.0"
)
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/14085/source/v2",
    "candidate_source_packet",
    "2.0",
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/14085/screening/v2",
    "candidate_test_patch",
    "2.0",
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/14085/preannotation/v2", "source_atomicity", "2.0"
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/14085/test_manifest/v2", "candidate_stage", "2.0"
)
_CHECKPOINT = ArtifactReference(
    "screening_checkpoint/luanti/14085/current/v2",
    "screening_checkpoint",
    "2.0",
)
_PRIOR_BLOCKER = ArtifactReference(
    "candidate_gate57_14085_preflight_blocker/luanti/14085/v1",
    "candidate_gate57_14085_preflight_blocker",
    "1.0",
)
_BLOCKER = ArtifactReference(
    "candidate_platform_admission_blocker/luanti/14085/windows/v1",
    "candidate_platform_admission_blocker",
    "1.0",
)

_PRODUCTION_PATH = "src/filesys.cpp"
_TEST_PATH = "src/unittest/test_filesys.cpp"
_RAW_PATHS = (
    "src/filesys.cpp",
    "src/unittest/CMakeLists.txt",
    "src/unittest/test_filesys.cpp",
)
_RUNNER_REFERENCES = {
    "windows_runner": "candidate_runner_registration/luanti/14085/windows-native/v1",
    "windows_toolchain": "candidate_toolchain_registration/luanti/14085/windows-native/v1",
    "windows_dependency_closure": "candidate_dependency_closure/luanti/14085/windows-native/v1",
    "windows_profile": "candidate_gate57_14085_windows_profile/luanti/14085/v1",
    "fixed_capsule": "fixed_source_capsule/luanti/14085/fixed/v2",
    "version_materialization": "candidate_stage/luanti/14085/version_materialization/v2",
}


def write_luanti_14085_platform_admission_blocker(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Reload the exact chain and persist a no-build Windows-runner blocker."""

    _require_authorization(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    source, patch, atomicity, manifest, checkpoint, prior = (
        _load(root, reference)
        for reference in (
            _SOURCE,
            _PATCH,
            _ATOMICITY,
            _MANIFEST,
            _CHECKPOINT,
            _PRIOR_BLOCKER,
        )
    )
    _validate_chain(source, patch, atomicity, manifest, checkpoint, prior)
    source_manifest = _read_json(
        _evidence_file(root, source.payload.get("raw_evidence_path"), "source manifest")
    )
    pull, issue = _validate_authority(source_manifest)
    raw_patch = _evidence_file(
        root, patch.payload.get("raw_patch_path"), "raw patch"
    ).read_text(encoding="utf-8", errors="surrogateescape")
    paths = _patch_paths(raw_patch)
    if paths != _RAW_PATHS:
        raise RuntimeError("#14085 raw patch inventory drifted")
    for marker in (
        "#ifdef _WIN32",
        "FlushFileBuffers",
        "#else",
        "void TestFileSys::testSafeWriteToFile()",
        "fs::safeWriteToFile(dest_path, test_data)",
    ):
        if marker not in raw_patch:
            raise RuntimeError(f"#14085 platform marker drifted: {marker}")
    if "bool safeWriteToFile(const std::string &path" not in raw_patch:
        raise RuntimeError("#14085 buggy-visible API context is missing")

    registrations = {
        name: (root / "artifacts" / Path(artifact_id)).is_dir()
        for name, artifact_id in _RUNNER_REFERENCES.items()
    }
    if any(registrations.values()):
        raise RuntimeError("#14085 platform closure appeared; re-audit required")
    host_platform = platform.system()
    host_arch = platform.machine()
    if host_platform == "Windows":
        raise RuntimeError("#14085 host became Windows; runner registration audit required")

    payload: dict[str, Any] = {
        "candidate": _CANDIDATE,
        "state": "blocked",
        "outcome": "platform_admission_blocked",
        "terminal": False,
        "resumable": True,
        "reason": "windows_only_regression_has_no_registered_windows_native_runner",
        "does_not_invalidate_gate_1_4": True,
        "current_gate_status": {
            "gate_1_source_authority": "passed",
            "gate_2_single_defect_mapping": "passed",
            "gate_3_strict_inverse_patch": "passed",
            "gate_4_fixed_buggy_test_api_compatibility": "passed",
            "platform_admission": "blocked_no_windows_native_runner",
            "gate_5_7": "not_run",
        },
        "authority": {
            "primary_issue_number": 9425,
            "pull_title": pull["title"],
            "pull_labels": _labels(pull),
            "issue_title": issue["title"],
            "issue_labels": _labels(issue),
            "defect_platform": "Windows",
        },
        "patch_audit": {
            "production_paths": [_PRODUCTION_PATH],
            "test_paths": [_TEST_PATH],
            "raw_patch_paths": list(paths),
            "production_only_inverse_paths": [_PRODUCTION_PATH],
            "test_support_paths_preserved": [
                "src/unittest/CMakeLists.txt",
                _TEST_PATH,
            ],
            "preserved_test_api": "fs::safeWriteToFile",
            "fixed_only_test_api": False,
            "compile_guard": "_WIN32",
            "fixed_operation": "FlushFileBuffers",
        },
        "platform_preflight": {
            "host_platform": host_platform,
            "host_arch": host_arch,
            "darwin_posix_runner_discriminatory": False,
            "windows_native_runner_required": True,
            "registration_references": _RUNNER_REFERENCES,
            "registrations_present": registrations,
            "registered_windows_closure_complete": False,
        },
        "truth_table": {
            "fixed": {"f2p": "pass", "p2p": "pass"},
            "buggy": {"f2p": "failure_on_windows_only", "p2p": "pass"},
            "darwin_posix_expected": {
                "fixed_f2p": "pass",
                "buggy_f2p": "pass_false_negative",
            },
            "attempt_count": 0,
            "required_attempt_count": 12,
        },
        "candidate_execution_performed": False,
        "candidate_materialization_performed": False,
        "new_build_started": False,
        "posix_false_negative_build_started": False,
        "old_attempts_loaded": False,
        "roster_modified": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "next_action": "register_candidate_specific_windows_native_runner_toolchain_and_dependency_closure_then_reaudit_platform_admission",
        "complete": True,
    }
    inputs = (_SOURCE, _PATCH, _ATOMICITY, _MANIFEST, _CHECKPOINT, _PRIOR_BLOCKER)
    existing = _try_load(root)
    if existing is not None:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError("#14085 existing platform blocker drifted")
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
        raise RuntimeError("#14085 platform blocker typed reload failed")
    return artifact


def _validate_chain(
    source: StableArtifact,
    patch: StableArtifact,
    atomicity: StableArtifact,
    manifest: StableArtifact,
    checkpoint: StableArtifact,
    prior: StableArtifact,
) -> None:
    if any(
        artifact.payload.get("candidate") != _CANDIDATE
        for artifact in (source, patch, atomicity, manifest, checkpoint, prior)
    ):
        raise RuntimeError("#14085 current-v2 candidate binding drifted")
    details = manifest.payload.get("details")
    prior_status = prior.payload.get("current_gate_status")
    if (
        source.inputs != (_ENUMERATION,)
        or source.payload.get("complete") is not True
        or patch.inputs != (_ENUMERATION, _SOURCE)
        or patch.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
        or patch.payload.get("production_paths") != [_PRODUCTION_PATH]
        or patch.payload.get("test_paths") != [_TEST_PATH]
        or checkpoint.inputs != (_ENUMERATION, _PATCH)
        or checkpoint.payload.get("state") != "complete"
        or checkpoint.payload.get("outcome") != "patch_eligible"
        or atomicity.inputs != (_ENUMERATION, _SOURCE, _PATCH, _CHECKPOINT)
        or atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("primary_issue_number") != 9425
        or atomicity.payload.get("authoritative_issue_numbers") != [9425]
        or manifest.inputs != (_PATCH,)
        or manifest.payload.get("state") != "passed"
        or type(details) is not dict
        or details.get("executable_test_paths") != [_TEST_PATH]
        or details.get("test_count") != 1
        or prior.payload.get("reason")
        != "windows_only_regression_has_no_registered_windows_native_runner"
        or type(prior_status) is not dict
        or any(
            prior_status.get(key) != "passed"
            for key in (
                "gate_1_source_authority",
                "gate_2_single_defect_mapping",
                "gate_3_strict_inverse_patch",
                "gate_4_fixed_test_api_compatibility",
            )
        )
        or prior.payload.get("candidate_execution_performed") is not False
        or prior.payload.get("old_attempts_loaded") is not False
    ):
        raise RuntimeError("#14085 current-v2 platform chain drifted")


def _validate_authority(
    source_manifest: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    pull = source_manifest.get("pull")
    issues = source_manifest.get("issues")
    row = (
        next((item for item in issues if item.get("number") == 9425), None)
        if type(issues) is list
        else None
    )
    issue = row.get("issue") if type(row) is dict else None
    if (
        source_manifest.get("candidate") != _CANDIDATE
        or source_manifest.get("issue_numbers") != [9425]
        or type(pull) is not dict
        or pull.get("number") != 14085
        or pull.get("merged") is not True
        or type(issue) is not dict
        or "Windows" not in _labels(pull)
        or "Bugfix 🐛" not in _labels(pull)
        or "Windows" not in _labels(issue)
        or "Bug" not in _labels(issue)
        or "Fixes https://github.com/minetest/minetest/issues/9425"
        not in str(pull.get("body"))
    ):
        raise RuntimeError("#14085 Windows defect authority drifted")
    return pull, issue


def _patch_paths(content: str) -> tuple[str, ...]:
    paths: list[str] = []
    for line in content.splitlines():
        if not line.startswith("diff --git "):
            continue
        parts = line.split(" ")
        if len(parts) != 4 or not parts[3].startswith("b/"):
            raise RuntimeError("#14085 raw patch header is invalid")
        path = _safe_relative(parts[3][2:], "patch path").as_posix()
        paths.append(path)
    return tuple(paths)


def _labels(value: dict[str, Any]) -> list[str]:
    return [
        item["name"]
        for item in value.get("labels", [])
        if type(item) is dict and type(item.get("name")) is str
    ]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("#14085 source manifest is invalid") from error
    if type(value) is not dict:
        raise RuntimeError("#14085 source manifest is not an object")
    return value


def _evidence_file(root: Path, value: object, label: str) -> Path:
    relative = _safe_relative(value, label)
    path = root.joinpath(*relative.parts)
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"#14085 {label} is unsafe")
    return path


def _safe_relative(value: object, label: str) -> PurePosixPath:
    if type(value) is not str or not value:
        raise RuntimeError(f"#14085 {label} path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or "." in path.parts or ".." in path.parts:
        raise RuntimeError(f"#14085 {label} path is unsafe")
    return path


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
        if str(error) in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            return None
        raise


def _require_authorization(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "candidate-screening"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#14085 platform blocker requires candidate-screening authorization")


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["write_luanti_14085_platform_admission_blocker"]
