"""Typed current-v2 Gate 1--4 semantic terminal for Luanti #14543.

The frozen source chain has one authoritative issue and a buggy-visible test
API, but both the issue and the pull request explicitly describe a feature:
game-ID aliases for complete game renaming.  The production-only inverse would
remove that capability rather than reproduce an authoritative defect.  This
module records the terminal before capsule materialization or execution.
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


_CANDIDATE = {"repository_key": "luanti", "change_number": 14543}
_ENUMERATION = ArtifactReference(
    "repository_enumeration/luanti/current/v2", "repository_enumeration", "2.0"
)
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/14543/source/v2",
    "candidate_source_packet",
    "2.0",
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/14543/screening/v2",
    "candidate_test_patch",
    "2.0",
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/14543/preannotation/v2", "source_atomicity", "2.0"
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/14543/test_manifest/v2", "candidate_stage", "2.0"
)
_CHECKPOINT = ArtifactReference(
    "screening_checkpoint/luanti/14543/current/v2",
    "screening_checkpoint",
    "2.0",
)
_TERMINAL = ArtifactReference(
    "candidate_gate14_semantic_terminal/luanti/14543/preannotation/v1",
    "candidate_gate14_semantic_terminal",
    "1.0",
)

_PRODUCTION_PATHS = (
    "builtin/mainmenu/content/contentdb.lua",
    "builtin/mainmenu/content/pkgmgr.lua",
    "builtin/mainmenu/init.lua",
    "src/content/subgames.cpp",
    "src/content/subgames.h",
    "src/script/lua_api/l_mainmenu.cpp",
)
_TEST_PATHS = ("builtin/mainmenu/content/tests/pkgmgr_spec.lua",)
_RAW_PATHS = (
    "builtin/mainmenu/content/contentdb.lua",
    "builtin/mainmenu/content/pkgmgr.lua",
    "builtin/mainmenu/content/tests/pkgmgr_spec.lua",
    "builtin/mainmenu/init.lua",
    "doc/lua_api.md",
    "doc/menu_lua_api.md",
    "src/content/subgames.cpp",
    "src/content/subgames.h",
    "src/script/lua_api/l_mainmenu.cpp",
)
_BUGGY_VISIBLE_API = "pkgmgr.install_dir"


def write_luanti_14543_gate14_semantic_terminal(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Strictly audit the current-v2 inputs and persist the feature terminal."""

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
    pull, issue = _validate_authority(source_manifest)
    raw_patch = _evidence_file(
        root, patch.payload.get("raw_patch_path"), "raw patch"
    ).read_bytes()
    sections = _parse_patch(raw_patch)
    if tuple(sections) != _RAW_PATHS:
        raise RuntimeError("#14543 raw patch inventory drifted")

    pkgmgr_lines = sections["builtin/mainmenu/content/pkgmgr.lua"]
    test_lines = sections[_TEST_PATHS[0]]
    preserved_calls = tuple(
        line[1:]
        for line in test_lines
        if line.startswith("+")
        and not line.startswith("+++")
        and _BUGGY_VISIBLE_API in line
    )
    buggy_definition_context = any(
        line.startswith("@@ ") and "function pkgmgr.install_dir" in line
        for line in pkgmgr_lines
    )
    fixed_only_symbols = (
        "pkgmgr.normalize_game_id",
        "game.aliases",
        "normalizeGameId",
    )
    test_fixed_only_references = tuple(
        symbol
        for symbol in fixed_only_symbols
        if any(
            line.startswith("+")
            and not line.startswith("+++")
            and symbol in line
            for line in test_lines
        )
    )
    if len(preserved_calls) != 1 or not buggy_definition_context:
        raise RuntimeError("#14543 buggy-visible test API evidence drifted")
    if test_fixed_only_references:
        raise RuntimeError("#14543 unexpected fixed-only test API appeared")

    omitted_paths = tuple(
        path
        for path in sections
        if path not in set(_PRODUCTION_PATHS + _TEST_PATHS)
    )
    if omitted_paths != ("doc/lua_api.md", "doc/menu_lua_api.md"):
        raise RuntimeError("#14543 typed code partition drifted")

    exact_namespaces = {
        "candidate_profile": root / "artifacts/candidate_profile/luanti/14543",
        "candidate_preparation": root
        / "artifacts/candidate_preparation/luanti/14543",
        "fixed_source_capsule": root
        / "artifacts/fixed_source_capsule/luanti/14543",
        "candidate_gate57": root / "artifacts/candidate_gate57/luanti/14543",
    }
    present = {name: path.exists() for name, path in exact_namespaces.items()}
    if any(present.values()):
        raise RuntimeError("#14543 downstream namespace appeared; re-audit required")

    payload: dict[str, Any] = {
        "candidate": _CANDIDATE,
        "state": "terminal",
        "outcome": "quality_rejected",
        "terminal": True,
        "resumable": False,
        "reason": "authoritative_feature_request_not_single_defect_regression",
        "classification": "game_id_alias_capability_addition",
        "primary_issue_number": 13662,
        "authoritative_issue_count": 1,
        "pull_title": pull["title"],
        "pull_labels": _labels(pull),
        "issue_title": issue["title"],
        "issue_labels": _labels(issue),
        "authority_markers": {
            "pull_body": "Allows defining gameid aliases, so that a game can be completely renamed.",
            "issue_solution": "Support game renaming through old game IDs and rebranding flow",
            "pull_feature_label": "Feature ✨",
            "issue_feature_label": "Feature request",
        },
        "current_gate_status": {
            "source_authority": "passed",
            "mechanical_single_issue_atomicity": "passed",
            "single_defect_mapping": "failed_feature_request_capability_addition",
            "strict_inverse_patch": "not_applicable_feature",
            "test_api_compatibility": "passed_static_buggy_visible_api_but_not_admitted",
            "fresh_gate57_epoch": "blocked_before_materialization",
        },
        "patch_audit": {
            "declared_production_paths": list(_PRODUCTION_PATHS),
            "declared_test_paths": list(_TEST_PATHS),
            "raw_patch_paths": list(sections),
            "documentation_paths_outside_typed_code_inventory": list(omitted_paths),
            "preserved_test_api": _BUGGY_VISIBLE_API,
            "preserved_test_api_call_count": len(preserved_calls),
            "preserved_test_api_call_examples": list(preserved_calls),
            "buggy_definition_observed_in_patch_context": buggy_definition_context,
            "fixed_only_test_api_references": list(test_fixed_only_references),
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
            raise RuntimeError("#14543 existing semantic terminal drifted")
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
        raise RuntimeError("#14543 semantic terminal typed reload failed")
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
        raise RuntimeError("#14543 current-v2 candidate binding drifted")
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
        or atomicity.payload.get("authoritative_issue_numbers") != [13662]
        or atomicity.payload.get("authoritative_issue_count") != 1
        or atomicity.payload.get("primary_issue_number") != 13662
        or atomicity.payload.get("linked_issue_numbers") != [13662]
        or manifest.inputs != (_PATCH,)
        or manifest.payload.get("complete") is not True
        or manifest.payload.get("stage") != "test_manifest"
        or manifest.payload.get("state") != "passed"
        or type(details) is not dict
        or details.get("executable_test_paths") != list(_TEST_PATHS)
        or details.get("test_count") != 1
    ):
        raise RuntimeError("#14543 current-v2 typed chain drifted")


def _validate_authority(
    source_manifest: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    pull = source_manifest.get("pull")
    issues = source_manifest.get("issues")
    row = (
        next((item for item in issues if item.get("number") == 13662), None)
        if type(issues) is list
        else None
    )
    issue = row.get("issue") if type(row) is dict else None
    if (
        source_manifest.get("candidate") != _CANDIDATE
        or source_manifest.get("complete") is not True
        or source_manifest.get("issue_numbers") != [13662]
        or type(pull) is not dict
        or pull.get("number") != 14543
        or pull.get("merged") is not True
        or pull.get("changed_files") != 9
        or type(issue) is not dict
        or "Feature ✨" not in _labels(pull)
        or "Feature request" not in _labels(issue)
        or "Allows defining gameid aliases" not in str(pull.get("body"))
        or "Fixes #13662" not in str(pull.get("body"))
        or "Support game renaming" != issue.get("title")
    ):
        raise RuntimeError("#14543 feature authority evidence drifted")
    return pull, issue


def _parse_patch(content: bytes) -> dict[str, tuple[str, ...]]:
    if not content.startswith(b"diff --git "):
        raise RuntimeError("#14543 raw patch is invalid")
    result: dict[str, list[str]] = {}
    current: str | None = None
    for line in content.decode("utf-8", errors="surrogateescape").splitlines():
        if line.startswith("diff --git "):
            parts = shlex.split(line)
            if len(parts) != 4 or parts[:2] != ["diff", "--git"]:
                raise RuntimeError("#14543 raw patch header is invalid")
            left, right = parts[2], parts[3]
            if not left.startswith("a/") or right != "b/" + left[2:]:
                raise RuntimeError("#14543 raw patch path binding is invalid")
            path = _safe_relative(left[2:], "patch path").as_posix()
            if path in result:
                raise RuntimeError("#14543 raw patch has duplicate path")
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
        raise RuntimeError("#14543 source manifest is invalid") from error
    if type(value) is not dict:
        raise RuntimeError("#14543 source manifest is not an object")
    return value


def _evidence_file(root: Path, value: object, label: str) -> Path:
    relative = _safe_relative(value, label)
    path = root.joinpath(*relative.parts)
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"#14543 {label} is unsafe")
    return path


def _safe_relative(value: object, label: str) -> PurePosixPath:
    if type(value) is not str or not value:
        raise RuntimeError(f"#14543 {label} path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or "." in path.parts or ".." in path.parts:
        raise RuntimeError(f"#14543 {label} path is unsafe")
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
        raise PermissionError("#14543 terminal requires candidate-screening authorization")


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["write_luanti_14543_gate14_semantic_terminal"]
