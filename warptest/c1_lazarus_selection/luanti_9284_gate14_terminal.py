"""Typed current-v2 Gate 1--4 terminal for Luanti #9284.

Issue #6711 and the pull request describe API capability additions, not an
authoritative defect regression.  Independently, the preserved unit test calls
the new three-argument ``Settings::setDefault`` overload, which disappears
under a production-only inverse.  This terminal is written before any capsule,
profile, build, or candidate execution.
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


_CANDIDATE = {"repository_key": "luanti", "change_number": 9284}
_ENUMERATION = ArtifactReference(
    "repository_enumeration/luanti/current/v2", "repository_enumeration", "2.0"
)
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/9284/source/v2",
    "candidate_source_packet",
    "2.0",
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/9284/screening/v2",
    "candidate_test_patch",
    "2.0",
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/9284/preannotation/v2", "source_atomicity", "2.0"
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/9284/test_manifest/v2", "candidate_stage", "2.0"
)
_CHECKPOINT = ArtifactReference(
    "screening_checkpoint/luanti/9284/current/v2",
    "screening_checkpoint",
    "2.0",
)
_TERMINAL = ArtifactReference(
    "candidate_gate14_terminal/luanti/9284/preannotation/v1",
    "candidate_gate14_terminal",
    "1.0",
)

_PRODUCTION_PATHS = (
    "src/defaultsettings.cpp",
    "src/mapgen/mapgen.cpp",
    "src/mapgen/mapgen.h",
    "src/mapgen/mapgen_carpathian.cpp",
    "src/mapgen/mapgen_carpathian.h",
    "src/mapgen/mapgen_flat.cpp",
    "src/mapgen/mapgen_flat.h",
    "src/mapgen/mapgen_fractal.cpp",
    "src/mapgen/mapgen_fractal.h",
    "src/mapgen/mapgen_v5.cpp",
    "src/mapgen/mapgen_v5.h",
    "src/mapgen/mapgen_v6.cpp",
    "src/mapgen/mapgen_v6.h",
    "src/mapgen/mapgen_v7.cpp",
    "src/mapgen/mapgen_v7.h",
    "src/mapgen/mapgen_valleys.cpp",
    "src/mapgen/mapgen_valleys.h",
    "src/script/lua_api/l_settings.cpp",
    "src/script/lua_api/l_settings.h",
    "src/settings.cpp",
    "src/settings.h",
)
_TEST_PATHS = ("src/unittest/test_settings.cpp",)
_RAW_PATHS = (
    "doc/lua_api.txt",
    *_PRODUCTION_PATHS,
    *_TEST_PATHS,
)
_FIXED_ONLY_API = "Settings::setDefault(name, FlagDesc*, u32)"


def write_luanti_9284_gate14_terminal(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Strictly replay current-v2 Gate 2--4 and persist the terminal."""

    _require_authorization(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    source, patch, atomicity, manifest, checkpoint = (
        _load(root, reference)
        for reference in (_SOURCE, _PATCH, _ATOMICITY, _MANIFEST, _CHECKPOINT)
    )
    _validate_chain(source, patch, atomicity, manifest, checkpoint)
    source_manifest = _read_json(
        _evidence_file(root, source.payload.get("raw_evidence_path"), "source manifest")
    )
    pull, issue, related_comment = _validate_authority(source_manifest)
    raw_patch = _evidence_file(
        root, patch.payload.get("raw_patch_path"), "raw patch"
    ).read_bytes()
    sections = _parse_patch(raw_patch)
    if tuple(sections) != _RAW_PATHS:
        raise RuntimeError("#9284 raw patch inventory drifted")

    settings_cpp = sections["src/settings.cpp"]
    settings_h = sections["src/settings.h"]
    test_lines = sections[_TEST_PATHS[0]]
    overload_definition = any(
        line.startswith("+")
        and "void Settings::setDefault(const std::string &name, const FlagDesc *flagdesc," in line
        for line in settings_cpp
    )
    overload_declaration = any(
        line.startswith("+")
        and "void setDefault(const std::string &name, const FlagDesc *flagdesc, u32 flags);" in line
        for line in settings_h
    )
    preserved_calls = tuple(
        line[1:]
        for line in test_lines
        if line.startswith("+")
        and not line.startswith("+++")
        and "s.setDefault(" in line
    )
    if not overload_definition or not overload_declaration or len(preserved_calls) != 2:
        raise RuntimeError("#9284 fixed-only Settings API evidence drifted")

    typed_paths = set(_PRODUCTION_PATHS + _TEST_PATHS)
    omitted_paths = tuple(path for path in sections if path not in typed_paths)
    if omitted_paths != ("doc/lua_api.txt",):
        raise RuntimeError("#9284 typed code partition drifted")

    exact_namespaces = {
        "candidate_profile": root / "artifacts/candidate_profile/luanti/9284",
        "candidate_preparation": root
        / "artifacts/candidate_preparation/luanti/9284",
        "fixed_source_capsule": root
        / "artifacts/fixed_source_capsule/luanti/9284",
        "candidate_gate57": root / "artifacts/candidate_gate57/luanti/9284",
    }
    present = {name: path.exists() for name, path in exact_namespaces.items()}
    if any(present.values()):
        raise RuntimeError("#9284 downstream namespace appeared; re-audit required")

    payload: dict[str, Any] = {
        "candidate": _CANDIDATE,
        "state": "terminal",
        "outcome": "quality_rejected",
        "terminal": True,
        "resumable": False,
        "reason": (
            "authoritative_feature_request_and_preserved_test_calls_fixed_only_API"
        ),
        "classification": "settings_flags_read_API_capability_addition",
        "primary_issue_number": 6711,
        "authoritative_issue_count": 1,
        "pull_title": pull["title"],
        "pull_labels": _labels(pull),
        "issue_title": issue["title"],
        "issue_labels": _labels(issue),
        "authority_markers": {
            "pull_feature_label": "Feature ✨",
            "issue_feature_label": "Feature request",
            "pull_goal": "API function to read the currently active flags",
            "issue_request": "add a function to grab the default value of a setting",
            "relationship_comment": related_comment,
            "pull_explicitly_fixes_primary_issue": False,
        },
        "current_gate_status": {
            "source_authority": "passed",
            "mechanical_single_issue_atomicity": "passed",
            "single_defect_mapping": "failed_feature_request_capability_addition",
            "strict_inverse_patch": "not_applicable_feature",
            "test_api_compatibility": "blocked_fixed_only_api",
            "fresh_gate57_epoch": "blocked_before_materialization",
        },
        "patch_audit": {
            "declared_production_paths": list(_PRODUCTION_PATHS),
            "declared_test_paths": list(_TEST_PATHS),
            "raw_patch_paths": list(sections),
            "documentation_paths_outside_typed_code_inventory": list(omitted_paths),
            "fixed_only_api": _FIXED_ONLY_API,
            "fixed_only_api_added_in_production_paths": [
                "src/settings.cpp",
                "src/settings.h",
            ],
            "preserved_test_call_count": len(preserved_calls),
            "preserved_test_call_examples": list(preserved_calls),
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
        "next_action": "do_not_admit_current_candidate_as_a_C1_defect_regression",
        "complete": True,
    }
    inputs = (_SOURCE, _PATCH, _ATOMICITY, _MANIFEST, _CHECKPOINT)
    existing = _try_load(root)
    if existing is not None:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError("#9284 existing Gate 1--4 terminal drifted")
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
        raise RuntimeError("#9284 Gate 1--4 terminal typed reload failed")
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
        raise RuntimeError("#9284 current-v2 candidate binding drifted")
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
        or atomicity.payload.get("authoritative_issue_numbers") != [6711]
        or atomicity.payload.get("authoritative_issue_count") != 1
        or atomicity.payload.get("primary_issue_number") != 6711
        or atomicity.payload.get("linked_issue_numbers") != [6711]
        or manifest.inputs != (_PATCH,)
        or manifest.payload.get("complete") is not True
        or manifest.payload.get("stage") != "test_manifest"
        or manifest.payload.get("state") != "passed"
        or type(details) is not dict
        or details.get("executable_test_paths") != list(_TEST_PATHS)
        or details.get("test_count") != 1
    ):
        raise RuntimeError("#9284 current-v2 typed chain drifted")


def _validate_authority(
    source_manifest: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], str]:
    pull = source_manifest.get("pull")
    issues = source_manifest.get("issues")
    row = (
        next((item for item in issues if item.get("number") == 6711), None)
        if type(issues) is list
        else None
    )
    issue = row.get("issue") if type(row) is dict else None
    comments = row.get("comments") if type(row) is dict else None
    related = next(
        (
            comment.get("body")
            for comment in comments
            if type(comment) is dict
            and type(comment.get("body")) is str
            and "Related #9284" in comment["body"]
        ),
        None,
    ) if type(comments) is list else None
    if (
        source_manifest.get("candidate") != _CANDIDATE
        or source_manifest.get("complete") is not True
        or source_manifest.get("issue_numbers") != [6711]
        or type(pull) is not dict
        or pull.get("number") != 9284
        or pull.get("merged") is not True
        or pull.get("changed_files") != 23
        or type(issue) is not dict
        or type(related) is not str
        or "Feature ✨" not in _labels(pull)
        or "Feature request" not in _labels(issue)
        or "API function to read the currently active flags" not in str(pull.get("body"))
        or "Fixes #6711" in str(pull.get("body"))
        or issue.get("title") != "Get default setting value from settingtypes.txt"
    ):
        raise RuntimeError("#9284 feature authority evidence drifted")
    return pull, issue, related


def _parse_patch(content: bytes) -> dict[str, tuple[str, ...]]:
    if not content.startswith(b"diff --git "):
        raise RuntimeError("#9284 raw patch is invalid")
    result: dict[str, list[str]] = {}
    current: str | None = None
    for line in content.decode("utf-8", errors="surrogateescape").splitlines():
        if line.startswith("diff --git "):
            parts = shlex.split(line)
            if len(parts) != 4 or parts[:2] != ["diff", "--git"]:
                raise RuntimeError("#9284 raw patch header is invalid")
            left, right = parts[2], parts[3]
            if not left.startswith("a/") or right != "b/" + left[2:]:
                raise RuntimeError("#9284 raw patch path binding is invalid")
            path = _safe_relative(left[2:], "patch path").as_posix()
            if path in result:
                raise RuntimeError("#9284 raw patch has duplicate path")
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


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("#9284 source manifest is invalid") from error
    if type(value) is not dict:
        raise RuntimeError("#9284 source manifest is not an object")
    return value


def _evidence_file(root: Path, value: object, label: str) -> Path:
    relative = _safe_relative(value, label)
    path = root.joinpath(*relative.parts)
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"#9284 {label} is unsafe")
    return path


def _safe_relative(value: object, label: str) -> PurePosixPath:
    if type(value) is not str or not value:
        raise RuntimeError(f"#9284 {label} path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or "." in path.parts or ".." in path.parts:
        raise RuntimeError(f"#9284 {label} path is unsafe")
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
        raise PermissionError("#9284 terminal requires candidate-screening authorization")


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["write_luanti_9284_gate14_terminal"]
