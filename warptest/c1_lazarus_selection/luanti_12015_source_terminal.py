"""Candidate-specific source-scope terminal for Luanti #12015.

The authoritative source manifest contains a single compiler-warning issue,
but PR #12015 is a 53-file omnibus ``Various minor fixes`` change.  Its own
description enumerates unrelated CI, XDG, Windows-name, shader, test and
formatting work.  It therefore cannot be reduced to the single issue without
discarding production changes, so it fails the source/one-defect admission
before a profile, capsule, build, or test attempt is permissible.
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


_CANDIDATE = {"repository_key": "luanti", "change_number": 12015}
_ENUMERATION = ArtifactReference(
    "repository_enumeration/luanti/current/v2", "repository_enumeration", "2.0"
)
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/12015/source/v2", "candidate_source_packet", "2.0"
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/12015/screening/v2", "candidate_test_patch", "2.0"
)
_CHECKPOINT = ArtifactReference(
    "screening_checkpoint/luanti/12015/current/v2", "screening_checkpoint", "2.0"
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/12015/preannotation/v2", "source_atomicity", "2.0"
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/12015/test_manifest/v2", "candidate_stage", "2.0"
)
_TERMINAL = ArtifactReference(
    "candidate_source_terminal/luanti/12015/current-v2/v1",
    "candidate_source_terminal",
    "1.0",
)
_SOURCE_MANIFEST = PurePosixPath(
    "raw/github/luanti/12015/source-manifest/attempt-1/2026-08-13T09-02-05.493218Z.json"
)
_RAW_PATCH = PurePosixPath(
    "raw/github/luanti/12015/pull-diff/attempt-1/2026-08-13T09-01-58.838941Z.response.body"
)


def write_luanti_12015_source_terminal(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Write a typed source-scope terminal without creating a test epoch."""

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "candidate-screening"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#12015 terminal requires active candidate-screening authorization")
    root = Path(artifact_root).absolute().resolve(strict=True)
    enumeration, source, patch, checkpoint, atomicity, manifest = tuple(
        _load(root, reference)
        for reference in (_ENUMERATION, _SOURCE, _PATCH, _CHECKPOINT, _ATOMICITY, _MANIFEST)
    )
    _validate_typed_chain(enumeration, source, patch, checkpoint, atomicity, manifest)
    source_path = _relative(source.payload.get("raw_evidence_path"), "source packet")
    patch_path = _relative(patch.payload.get("raw_patch_path"), "patch receipt")
    source_manifest = _read_json(root / source_path, "source manifest")
    raw_patch = _read_text(root / patch_path, "raw patch")
    pull, issue = _validate_authoritative_source(source_manifest)
    patch_paths = _patch_paths(raw_patch)
    _validate_omnibus_scope(pull, issue, patch_paths)
    _assert_absent(root)

    absent_gate57_assets = {
        "candidate_profile": "artifacts/candidate_profile/luanti/12015",
        "fixed_source_capsule": "artifacts/fixed_source_capsule/luanti/12015",
        "gate57_toolchain": "artifacts/candidate_gate57_fresh_toolchain/luanti/12015",
    }
    present = {name: (root / relative).exists() for name, relative in absent_gate57_assets.items()}
    if any(present.values()):
        raise RuntimeError("#12015 exact current-v2 namespaces changed; re-audit source scope")
    payload: dict[str, Any] = {
        "candidate": dict(_CANDIDATE),
        "state": "terminal",
        "outcome": "quality_rejected",
        "terminal": True,
        "resumable": False,
        "reason": "omnibus_pr_has_multiple_distinct_changes_beyond_single_authoritative_issue",
        "classification": "multi_defect_maintenance_bundle",
        "source_manifest_path": _SOURCE_MANIFEST.as_posix(),
        "raw_patch_path": _RAW_PATCH.as_posix(),
        "primary_issue_number": 10238,
        "authoritative_issue_count": 1,
        "authoritative_issue_title": issue.get("title"),
        "authoritative_issue_labels": sorted(_labels(issue)),
        "authoritative_issue_scope": "gcc_10_array_bounds_warning_in_src_util_srp_cpp",
        "pull_title": pull.get("title"),
        "pull_labels": sorted(_labels(pull)),
        "pull_changed_files": pull.get("changed_files"),
        "raw_patch_path_count": len(patch_paths),
        "raw_patch_paths": patch_paths,
        "distinct_unrelated_change_groups": {
            "ci_and_build": [path for path in patch_paths if path.startswith((".github/", ".gitlab", "cmake/", "util/ci/", "util/buildbot/", "CMakeLists"))],
            "documentation_and_config": [path for path in patch_paths if path in {"README.md", "doc/lgpl-2.1.txt", "minetest.conf.example"}],
            "engine_and_gameplay": [path for path in patch_paths if path.startswith(("src/client/", "src/mapgen/", "src/script/", "src/server"))],
            "unittests": [path for path in patch_paths if path.startswith("src/unittest/")],
        },
        "gate_status": {
            "gate_1_source_authority": "passed_raw_authority_but_pr_scope_non_atomic",
            "gate_2_real_single_bug_or_regression_mapping": "failed_multiple_distinct_changes",
            "gate_3_production_only_inverse": "not_admissible_omnibus_pr",
            "gate_4_fixed_test_buggy_api_compatibility": "not_admissible_omnibus_pr",
            "gate_5_fixed_f2p_3x": "not_run_source_terminal",
            "gate_6_buggy_f2p_3x_failure": "not_run_source_terminal",
            "gate_7_fixed_buggy_p2p_3x": "not_run_source_terminal",
        },
        "current_v2_asset_probe": {
            name: {"present": present[name], "path": relative}
            for name, relative in absent_gate57_assets.items()
        },
        "candidate_materialization_performed": False,
        "new_build_started": False,
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    write_stable_artifact(
        root,
        artifact_id=_TERMINAL.artifact_id,
        artifact_kind=_TERMINAL.artifact_kind,
        semantic_version=_TERMINAL.semantic_version,
        created_at=_now(),
        inputs=(_ENUMERATION, _SOURCE, _PATCH, _CHECKPOINT, _ATOMICITY, _MANIFEST),
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(root, _TERMINAL)
    if artifact.inputs != (_ENUMERATION, _SOURCE, _PATCH, _CHECKPOINT, _ATOMICITY, _MANIFEST) or dict(artifact.payload) != payload:
        raise RuntimeError("#12015 source terminal typed reload failed")
    return artifact


def _validate_authoritative_source(
    source_manifest: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
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
        or source_manifest.get("issue_numbers") != [10238]
    ):
        raise RuntimeError("#12015 source manifest authority binding is invalid")
    pull = source_manifest.get("pull")
    if not isinstance(pull, dict) or pull.get("number") != 12015 or pull.get("merged") is not True:
        raise RuntimeError("#12015 pull source is invalid")
    for item in source_manifest.get("issues", []):
        if isinstance(item, dict) and item.get("number") == 10238 and isinstance(item.get("issue"), dict):
            return pull, item["issue"]
    raise RuntimeError("#12015 authoritative issue is missing")


def _validate_typed_chain(
    enumeration: StableArtifact,
    source: StableArtifact,
    patch: StableArtifact,
    checkpoint: StableArtifact,
    atomicity: StableArtifact,
    manifest: StableArtifact,
) -> None:
    if enumeration.payload.get("repository_key") != "luanti":
        raise RuntimeError("#12015 enumeration binding is not Luanti")
    if (
        source.inputs != (_ENUMERATION,)
        or source.payload.get("candidate") != _CANDIDATE
        or source.payload.get("complete") is not True
        or source.payload.get("raw_evidence_path") != _SOURCE_MANIFEST.as_posix()
    ):
        raise RuntimeError("#12015 source packet binding is invalid")
    if (
        patch.inputs != (_ENUMERATION, _SOURCE)
        or patch.payload.get("candidate") != _CANDIDATE
        or patch.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
        or patch.payload.get("raw_patch_path") != _RAW_PATCH.as_posix()
        or patch.payload.get("source_manifest_path") != _SOURCE_MANIFEST.as_posix()
        or len(patch.payload.get("production_paths", [])) != 32
        or patch.payload.get("test_paths")
        != ["src/unittest/test_irrptr.cpp", "src/unittest/test_utilities.cpp", "src/unittest/test_voxelarea.cpp"]
    ):
        raise RuntimeError("#12015 typed patch inventory is invalid")
    if (
        checkpoint.inputs != (_ENUMERATION, _PATCH)
        or checkpoint.payload.get("candidate") != _CANDIDATE
        or checkpoint.payload.get("state") != "complete"
        or checkpoint.payload.get("stage_artifact_id") != _PATCH.artifact_id
    ):
        raise RuntimeError("#12015 screening checkpoint is invalid")
    if (
        atomicity.inputs != (_ENUMERATION, _SOURCE, _PATCH, _CHECKPOINT)
        or atomicity.payload.get("candidate") != _CANDIDATE
        or atomicity.payload.get("complete") is not True
        or atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("authoritative_issue_numbers") != [10238]
        or atomicity.payload.get("primary_issue_number") != 10238
    ):
        raise RuntimeError("#12015 source atomicity receipt is invalid")
    details = manifest.payload.get("details")
    if (
        manifest.inputs != (_PATCH,)
        or manifest.payload.get("candidate") != _CANDIDATE
        or manifest.payload.get("complete") is not True
        or manifest.payload.get("stage") != "test_manifest"
        or manifest.payload.get("state") != "passed"
        or not isinstance(details, dict)
        or details.get("test_count") != 3
    ):
        raise RuntimeError("#12015 test manifest is invalid")


def _validate_omnibus_scope(
    pull: dict[str, Any], issue: dict[str, Any], patch_paths: list[str]
) -> None:
    body = pull.get("body")
    issue_body = issue.get("body")
    if not isinstance(body, str) or not isinstance(issue_body, str):
        raise RuntimeError("#12015 source bodies are invalid")
    if pull.get("title") != "[no squash] Various minor fixes" or pull.get("changed_files") != 53:
        raise RuntimeError("#12015 expected omnibus PR identity drifted")
    if len(patch_paths) != 53 or len(set(patch_paths)) != 53:
        raise RuntimeError("#12015 raw patch scope is not the expected 53 unique paths")
    if not {"Maintenance", "Code quality"}.issubset(_labels(pull)):
        raise RuntimeError("#12015 PR maintenance classification evidence is incomplete")
    if "Type: Build issue / warning" not in issue_body or "src/util/srp.cpp" not in issue_body:
        raise RuntimeError("#12015 authoritative warning issue scope is invalid")
    markers = (
        "Fix all<sup>[1]</sup> compiler warnings",
        "Fix `XDG_CACHE_HOME` spec compliance",
        "Add some additional names that are reserved on Windows",
        "Some test fixes",
        "Auto-detect build parallelism on CI",
    )
    if not all(marker in body for marker in markers):
        raise RuntimeError("#12015 diverse PR scope markers are incomplete")
    required_paths = {
        ".github/workflows/macos.yml",
        "README.md",
        "src/util/srp.cpp",
        "src/script/lua_api/l_env.cpp",
        "src/unittest/test_voxelarea.cpp",
        "util/ci/build.sh",
    }
    if not required_paths.issubset(patch_paths):
        raise RuntimeError("#12015 raw patch does not demonstrate unrelated change groups")


def _patch_paths(raw_patch: str) -> list[str]:
    paths: list[str] = []
    for line in raw_patch.splitlines():
        if line.startswith("diff --git a/"):
            try:
                left, right = line.removeprefix("diff --git a/").split(" b/", 1)
            except ValueError as error:
                raise RuntimeError("#12015 raw patch header is malformed") from error
            if left != right:
                raise RuntimeError("#12015 rename/copy patch needs a dedicated scope audit")
            paths.append(right)
    return paths


def _labels(value: dict[str, Any]) -> set[str]:
    labels = value.get("labels")
    if not isinstance(labels, list):
        raise RuntimeError("#12015 labels are invalid")
    return {item["name"] for item in labels if isinstance(item, dict) and isinstance(item.get("name"), str)}


def _relative(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise RuntimeError(f"#12015 {label} path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {".", ".."} for part in path.parts) or path.as_posix() != value:
        raise RuntimeError(f"#12015 {label} path is unsafe")
    return path.as_posix()


def _read_json(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"#12015 {label} is not a safe regular file")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"#12015 {label} is invalid")
    return value


def _read_text(path: Path, label: str) -> str:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"#12015 {label} is not a safe regular file")
    return path.read_text(encoding="utf-8", errors="surrogateescape")


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _assert_absent(root: Path) -> None:
    path = root / "artifacts" / Path(_TERMINAL.artifact_id) / _TERMINAL.semantic_version
    if path.exists():
        raise RuntimeError("#12015 typed source terminal already exists")


def _now() -> str:
    return (datetime.now(UTC) + timedelta(microseconds=1)).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["write_luanti_12015_source_terminal"]
