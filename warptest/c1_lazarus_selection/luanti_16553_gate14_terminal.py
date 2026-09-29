"""Typed current-v2 Gate 1--4 terminal for Luanti #16553.

The mechanical atomicity receipt has one authoritative issue, but the frozen
authority describes a feature/request: add builtin-file consistency warnings.
The current patch contract is independently unsafe for a production-only
inverse because the preserved unit test calls ``fs::MakePathRelativeTo``, an
API introduced by the production patch, and the typed production inventory
omits essential build/generated-source hunks.  No capsule, build, candidate
execution, paid annotation, or formal trial is permitted from this receipt.
"""

from __future__ import annotations

import json
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


_CANDIDATE = {"repository_key": "luanti", "change_number": 16553}
_ENUMERATION = ArtifactReference(
    "repository_enumeration/luanti/current/v2", "repository_enumeration", "2.0"
)
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/16553/source/v2",
    "candidate_source_packet",
    "2.0",
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/16553/screening/v2",
    "candidate_test_patch",
    "2.0",
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/16553/preannotation/v2", "source_atomicity", "2.0"
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/16553/test_manifest/v2", "candidate_stage", "2.0"
)
_CHECKPOINT = ArtifactReference(
    "screening_checkpoint/luanti/16553/current/v2",
    "screening_checkpoint",
    "2.0",
)
_TERMINAL = ArtifactReference(
    "candidate_gate14_terminal/luanti/16553/preannotation/v1",
    "candidate_gate14_terminal",
    "1.0",
)

_PRODUCTION_PATHS = (
    "src/builtin_files.h",
    "src/filesys.cpp",
    "src/filesys.h",
    "src/script/cpp_api/s_base.cpp",
    "src/script/cpp_api/s_security.cpp",
    "src/script/cpp_api/s_security.h",
)
_TEST_PATHS = ("src/unittest/test_filesys.cpp",)
_FIXED_ONLY_API = "MakePathRelativeTo"
_RAW_PATHS = (
    "CMakeLists.txt",
    "builtin/CMakeLists.txt",
    "builtin/async/CMakeLists.txt",
    "builtin/client/CMakeLists.txt",
    "builtin/common/CMakeLists.txt",
    "builtin/common/settings/CMakeLists.txt",
    "builtin/emerge/CMakeLists.txt",
    "builtin/fstk/CMakeLists.txt",
    "builtin/game/CMakeLists.txt",
    "builtin/mainmenu/CMakeLists.txt",
    "builtin/mainmenu/content/CMakeLists.txt",
    "builtin/pause_menu/CMakeLists.txt",
    "builtin/profiler/CMakeLists.txt",
    "cmake/Modules/HashBuiltinFiles.cmake",
    "src/CMakeLists.txt",
    "src/builtin_files.cpp.in",
    "src/builtin_files.h",
    "src/filesys.cpp",
    "src/filesys.h",
    "src/script/cpp_api/s_base.cpp",
    "src/script/cpp_api/s_security.cpp",
    "src/script/cpp_api/s_security.h",
    "src/unittest/test_filesys.cpp",
    "util/ci/clang-tidy.sh",
)


def write_luanti_16553_gate14_terminal(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Audit the exact current-v2 chain and persist the no-execution terminal."""

    _require_authorization(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    source, patch, atomicity, manifest, checkpoint = (
        _load(root, reference)
        for reference in (_SOURCE, _PATCH, _ATOMICITY, _MANIFEST, _CHECKPOINT)
    )
    _validate_chain(source, patch, atomicity, manifest, checkpoint)
    source_manifest = _read_source_manifest(root, source)
    pull, issue = _validate_authority(source_manifest)
    raw_patch = _read_raw_patch(root, patch)
    sections = _parse_patch(raw_patch)
    if tuple(sections) != _RAW_PATHS:
        raise RuntimeError("#16553 raw patch path inventory drifted")
    additions = {
        path: tuple(
            line[1:]
            for line in lines
            if line.startswith("+") and not line.startswith("+++")
        )
        for path, lines in sections.items()
    }
    fixed_api_production_paths = sorted(
        path
        for path in _PRODUCTION_PATHS
        if any(_FIXED_ONLY_API in line for line in additions[path])
    )
    test_api_calls = tuple(
        line
        for line in additions[_TEST_PATHS[0]]
        if f"fs::{_FIXED_ONLY_API}(" in line
    )
    if fixed_api_production_paths != [
        "src/filesys.cpp",
        "src/filesys.h",
        "src/script/cpp_api/s_security.cpp",
    ]:
        raise RuntimeError("#16553 fixed-only API production binding drifted")
    if len(test_api_calls) != 1:
        raise RuntimeError("#16553 preserved fixed-only API call drifted")

    typed_paths = set(_PRODUCTION_PATHS + _TEST_PATHS)
    omitted_paths = tuple(path for path in sections if path not in typed_paths)
    required_omitted = {
        "builtin/CMakeLists.txt",
        "cmake/Modules/HashBuiltinFiles.cmake",
        "src/CMakeLists.txt",
        "src/builtin_files.cpp.in",
    }
    if not required_omitted.issubset(omitted_paths):
        raise RuntimeError("#16553 inverse-support path evidence drifted")

    exact_namespaces = {
        "candidate_profile": root / "artifacts/candidate_profile/luanti/16553",
        "candidate_preparation": root
        / "artifacts/candidate_preparation/luanti/16553",
        "fixed_source_capsule": root
        / "artifacts/fixed_source_capsule/luanti/16553",
        "candidate_gate57": root / "artifacts/candidate_gate57/luanti/16553",
    }
    present = {name: path.exists() for name, path in exact_namespaces.items()}
    if any(present.values()):
        raise RuntimeError("#16553 downstream namespace appeared; re-audit required")

    pull_labels = _labels(pull)
    issue_labels = _labels(issue)
    payload: dict[str, Any] = {
        "candidate": _CANDIDATE,
        "state": "terminal",
        "outcome": "quality_rejected",
        "terminal": True,
        "resumable": False,
        "reason": (
            "authoritative_feature_request_and_preserved_test_calls_fixed_only_API"
        ),
        "classification": "builtin_consistency_warning_capability_addition",
        "primary_issue_number": 16318,
        "authoritative_issue_count": 1,
        "linked_issue_numbers": [16318, 17354],
        "incidental_post_merge_issue_numbers": [17354],
        "pull_title": pull["title"],
        "pull_labels": pull_labels,
        "issue_title": issue["title"],
        "issue_labels": issue_labels,
        "authority_markers": {
            "pull_feature_label": "Feature ✨",
            "issue_request_label": "Request / Suggestion",
            "pull_body_capability": (
                "Prints warnings if you try to loadfile or dofile a path in builtin when the file list or content differs"
            ),
            "issue_requested_behavior": (
                "Have some sort of version check in builtin"
            ),
        },
        "current_gate_status": {
            "source_authority": "passed",
            "mechanical_single_issue_atomicity": "passed",
            "single_defect_mapping": "failed_feature_request_capability_addition",
            "strict_inverse_patch": "blocked_incomplete_typed_production_inventory",
            "test_api_compatibility": "blocked_fixed_only_api",
            "fresh_gate57_epoch": "blocked_before_materialization",
        },
        "patch_audit": {
            "declared_production_paths": list(_PRODUCTION_PATHS),
            "declared_test_paths": list(_TEST_PATHS),
            "raw_patch_paths": list(sections),
            "omitted_from_typed_code_inventory": list(omitted_paths),
            "required_inverse_support_paths_omitted": sorted(required_omitted),
            "fixed_only_api": f"fs::{_FIXED_ONLY_API}",
            "fixed_only_api_added_in_production_paths": fixed_api_production_paths,
            "preserved_test_call_count": len(test_api_calls),
            "preserved_test_call_examples": list(test_api_calls),
            "buggy_compile_expected": True,
        },
        "downstream_namespace_probe": {
            name: {"present": present[name], "path": path.as_posix()}
            for name, path in exact_namespaces.items()
        },
        "fresh_epoch": {
            "required_attempt_count": 12,
            "attempt_count": 0,
            "inherit_prior_attempts": False,
            "old_attempts_loaded": False,
        },
        "candidate_execution_performed": False,
        "candidate_materialization_performed": False,
        "new_build_started": False,
        "roster_modified": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "next_action": (
            "do_not_admit_current_candidate; a distinct defect-regression test contract using only buggy-visible APIs would require a new current-v2 audit"
        ),
        "complete": True,
    }
    inputs = (_SOURCE, _PATCH, _ATOMICITY, _MANIFEST, _CHECKPOINT)
    existing = _try_load(root)
    if existing is not None:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError("#16553 existing Gate 1--4 terminal drifted")
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
    artifact = _load(root, _TERMINAL)
    if artifact.inputs != inputs or dict(artifact.payload) != payload:
        raise RuntimeError("#16553 Gate 1--4 terminal typed reload failed")
    return artifact


def _validate_chain(
    source: StableArtifact,
    patch: StableArtifact,
    atomicity: StableArtifact,
    manifest: StableArtifact,
    checkpoint: StableArtifact,
) -> None:
    if any(
        artifact.payload.get("candidate") != _CANDIDATE
        for artifact in (source, patch, atomicity, manifest, checkpoint)
    ):
        raise RuntimeError("#16553 current-v2 candidate binding drifted")
    details = manifest.payload.get("details")
    if (
        source.inputs != (_ENUMERATION,)
        or source.payload.get("complete") is not True
        or patch.inputs != (_ENUMERATION, _SOURCE)
        or patch.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
        or tuple(patch.payload.get("production_paths", ())) != _PRODUCTION_PATHS
        or tuple(patch.payload.get("test_paths", ())) != _TEST_PATHS
        or checkpoint.inputs != (_ENUMERATION, _PATCH)
        or checkpoint.payload.get("state") != "complete"
        or checkpoint.payload.get("outcome") != "patch_eligible"
        or checkpoint.payload.get("stage_artifact_id") != _PATCH.artifact_id
        or atomicity.inputs != (_ENUMERATION, _SOURCE, _PATCH, _CHECKPOINT)
        or atomicity.payload.get("complete") is not True
        or atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("authoritative_issue_numbers") != [16318]
        or atomicity.payload.get("authoritative_issue_count") != 1
        or atomicity.payload.get("primary_issue_number") != 16318
        or atomicity.payload.get("linked_issue_numbers") != [16318, 17354]
        or manifest.inputs != (_PATCH,)
        or manifest.payload.get("complete") is not True
        or manifest.payload.get("stage") != "test_manifest"
        or manifest.payload.get("state") != "passed"
        or type(details) is not dict
        or details.get("executable_test_paths") != list(_TEST_PATHS)
        or details.get("test_count") != 1
    ):
        raise RuntimeError("#16553 current-v2 typed chain drifted")


def _read_source_manifest(root: Path, source: StableArtifact) -> dict[str, Any]:
    path = _evidence_file(root, source.payload.get("raw_evidence_path"), "source manifest")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("#16553 source manifest is invalid") from error
    if type(value) is not dict:
        raise RuntimeError("#16553 source manifest is not an object")
    return value


def _validate_authority(
    source_manifest: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    pull = source_manifest.get("pull")
    issues = source_manifest.get("issues")
    if (
        source_manifest.get("candidate") != _CANDIDATE
        or source_manifest.get("complete") is not True
        or source_manifest.get("provider") != "github"
        or source_manifest.get("authority")
        != {
            "api_base": "https://api.github.com",
            "provider": "github",
            "repository": "luanti-org/luanti",
        }
        or source_manifest.get("issue_numbers") != [16318, 17354]
        or type(pull) is not dict
        or pull.get("number") != 16553
        or pull.get("merged") is not True
        or pull.get("changed_files") != 24
        or type(issues) is not list
    ):
        raise RuntimeError("#16553 source authority chain drifted")
    row = next((item for item in issues if item.get("number") == 16318), None)
    issue = row.get("issue") if type(row) is dict else None
    body = pull.get("body")
    issue_body = issue.get("body") if type(issue) is dict else None
    if (
        type(issue) is not dict
        or type(body) is not str
        or type(issue_body) is not str
        or "Feature ✨" not in _labels(pull)
        or "Request / Suggestion" not in _labels(issue)
        or "Print warnings" not in pull.get("title", "")
        or "Fixes #16318" not in body
        or "Have some sort of version check in builtin" not in issue_body
    ):
        raise RuntimeError("#16553 feature/request classification drifted")
    return pull, issue


def _read_raw_patch(root: Path, patch: StableArtifact) -> bytes:
    path = _evidence_file(root, patch.payload.get("raw_patch_path"), "raw patch")
    content = path.read_bytes()
    if not content.startswith(b"diff --git "):
        raise RuntimeError("#16553 raw patch is invalid")
    return content


def _parse_patch(content: bytes) -> dict[str, tuple[str, ...]]:
    result: dict[str, list[str]] = {}
    current: str | None = None
    for line in content.decode("utf-8", errors="surrogateescape").splitlines():
        if line.startswith("diff --git "):
            parts = shlex.split(line)
            if len(parts) != 4 or parts[:2] != ["diff", "--git"]:
                raise RuntimeError("#16553 raw patch header is invalid")
            left, right = parts[2], parts[3]
            if not left.startswith("a/") or right != "b/" + left[2:]:
                raise RuntimeError("#16553 raw patch path binding is invalid")
            path = _safe_relative(left[2:], "patch path").as_posix()
            if path in result:
                raise RuntimeError("#16553 raw patch has duplicate path")
            result[path] = []
            current = path
        elif current is not None:
            result[current].append(line)
    return {path: tuple(lines) for path, lines in result.items()}


def _labels(value: dict[str, Any]) -> list[str]:
    return [
        item["name"]
        for item in value.get("labels", [])
        if type(item) is dict and type(item.get("name")) is str
    ]


def _evidence_file(root: Path, value: object, label: str) -> Path:
    relative = _safe_relative(value, label)
    path = root.joinpath(*relative.parts)
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"#16553 {label} is unsafe")
    return path


def _safe_relative(value: object, label: str) -> PurePosixPath:
    if type(value) is not str or not value:
        raise RuntimeError(f"#16553 {label} path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or "." in path.parts or ".." in path.parts:
        raise RuntimeError(f"#16553 {label} path is unsafe")
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
        return _load(root, _TERMINAL)
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
        raise PermissionError("#16553 terminal requires candidate-screening authorization")


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["write_luanti_16553_gate14_terminal"]
