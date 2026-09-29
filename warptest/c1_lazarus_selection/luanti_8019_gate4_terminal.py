"""Strict Gate 4 terminal proof for Luanti #8019.

The preserved Euler-conversion unit test calls four helpers introduced by the
same patch in ``src/util/numeric.{h,cpp}``.  The production-only inverse
removes them, so this test cannot compile against the buggy revision.
"""

from __future__ import annotations

import os
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


_CANDIDATE = {"repository_key": "luanti", "change_number": 8019}
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/8019/source/v2", "candidate_source_packet", "2.0"
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/8019/screening/v2", "candidate_test_patch", "2.0"
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/8019/preannotation/v2", "source_atomicity", "2.0"
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/8019/test_manifest/v2", "candidate_stage", "2.0"
)
_TERMINAL = ArtifactReference(
    "candidate_gate14_terminal/luanti/8019/preannotation/v1",
    "candidate_gate14_terminal",
    "1.0",
)
_PRODUCTION = (
    "src/client/content_cao.cpp",
    "src/client/content_cao.h",
    "src/irrlichttypes_extrabloated.h",
    "src/network/networkprotocol.h",
    "src/util/numeric.cpp",
    "src/util/numeric.h",
)
_TEST = "src/unittest/test_utilities.cpp"
_FIXED_ONLY_APIS = (
    "setPitchYawRollRad",
    "setPitchYawRoll",
    "getPitchYawRollRad",
    "getPitchYawRoll",
)


def audit_luanti_8019_gate4(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Persist a current-chain-bound API-incompatibility terminal receipt."""

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "candidate-screening"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#8019 Gate 4 requires active candidate-screening")
    root = Path(artifact_root).absolute().resolve(strict=True)
    source, patch, atomicity, manifest = tuple(
        _load(root, reference) for reference in (_SOURCE, _PATCH, _ATOMICITY, _MANIFEST)
    )
    _validate_chain(source, patch, atomicity, manifest)
    patch_path = _safe_relative(patch.payload.get("raw_patch_path"))
    raw_patch = _regular_file(root / patch_path).read_bytes()
    audit = _audit_patch(raw_patch)
    if not audit["incompatible"]:
        raise RuntimeError("#8019 expected fixed-only helper evidence is absent")
    payload: dict[str, Any] = {
        "candidate": dict(_CANDIDATE),
        "profile_id": "luanti-8019-euler-rotation-v1-native-gate57",
        "state": "terminal",
        "outcome": "gate4_incompatible",
        "terminal": True,
        "resumable": False,
        "reason": "preserved_fixed_test_calls_pr_introduced_euler_helpers_absent_after_production_inverse",
        "current_gate_status": {
            "source_authority": "passed",
            "single_defect_mapping": "passed_issue_7927_rotation_axes_regression",
            "strict_inverse_patch": "passed_mechanical",
            "test_api_compatibility": "failed_fixed_only_api",
            "fresh_gate57_epoch": "not_run_terminal",
        },
        "current_v2_chain_revisions": {
            artifact.reference.artifact_id: artifact.created_at
            for artifact in (source, patch, atomicity, manifest)
        },
        "patch_audit": audit,
        "candidate_materialization_performed": False,
        "candidate_execution_performed": False,
        "new_build_started": False,
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
        "next_action": "do_not_schedule_capsule_build_or_gate57_execution; remove_candidate_from_gate57_pool",
    }
    inputs = (_SOURCE, _PATCH, _ATOMICITY, _MANIFEST)
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
            raise RuntimeError("#8019 Gate 4 terminal conflicts with current evidence")
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
        raise RuntimeError("#8019 Gate 4 terminal typed reload failed")
    return result


def _validate_chain(
    source: StableArtifact,
    patch: StableArtifact,
    atomicity: StableArtifact,
    manifest: StableArtifact,
) -> None:
    details = manifest.payload.get("details")
    if (
        source.payload.get("candidate") != _CANDIDATE
        or source.payload.get("complete") is not True
        or patch.inputs[-1:] != (_SOURCE,)
        or patch.payload.get("candidate") != _CANDIDATE
        or patch.payload.get("outcome") != "patch_eligible"
        or tuple(patch.payload.get("production_paths", ())) != _PRODUCTION
        or tuple(patch.payload.get("test_paths", ())) != (_TEST,)
        or patch.payload.get("source_manifest_path")
        != source.payload.get("raw_evidence_path")
        or atomicity.payload.get("candidate") != _CANDIDATE
        or atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("authoritative_issue_count") != 1
        or atomicity.payload.get("primary_issue_number") != 7927
        or manifest.inputs != (_PATCH,)
        or manifest.payload.get("candidate") != _CANDIDATE
        or manifest.payload.get("stage") != "test_manifest"
        or manifest.payload.get("state") != "passed"
        or not isinstance(details, dict)
        or details.get("executable_test_paths") != [_TEST]
        or details.get("test_count") != 1
    ):
        raise RuntimeError("#8019 current-v2 chain drifted")


def _audit_patch(raw_patch: bytes) -> dict[str, object]:
    text = raw_patch.decode("utf-8", errors="surrogateescape")
    sections = _patch_sections(text)
    allowed_paths = set(_PRODUCTION + (_TEST, "doc/lua_api.txt"))
    if set(sections) != allowed_paths:
        raise RuntimeError("#8019 raw patch inventory drifted")
    production = "\n".join(
        line for path in _PRODUCTION for line in sections[path] if line.startswith("+")
    )
    test = "\n".join(line for line in sections[_TEST] if line.startswith("+"))
    introduced = {api: api in production for api in _FIXED_ONLY_APIS}
    test_calls = {api: api in test for api in _FIXED_ONLY_APIS}
    incompatible = all(introduced.values()) and all(test_calls.values())
    return {
        "declared_production_paths": list(_PRODUCTION),
        "declared_test_paths": [_TEST],
        "raw_patch_paths": sorted(sections),
        "documentation_only_excluded_path": "doc/lua_api.txt",
        "inverse_scope_strict": True,
        "introduced_production_apis": introduced,
        "preserved_test_calls": test_calls,
        "fixed_only_api": incompatible,
        "buggy_compile_expected": False,
        "incompatible": incompatible,
    }


def _patch_sections(text: str) -> dict[str, tuple[str, ...]]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("diff --git a/"):
            try:
                left, right = line.removeprefix("diff --git ").split(" b/", 1)
            except ValueError as error:
                raise RuntimeError("#8019 raw patch header is invalid") from error
            path = left.removeprefix("a/")
            if left != f"a/{path}" or right != path or path in sections:
                raise RuntimeError("#8019 raw patch path is invalid")
            current = path
            sections[path] = []
        if current is not None:
            sections[current].append(line)
    if not sections:
        raise RuntimeError("#8019 raw patch is empty")
    return {path: tuple(lines) for path, lines in sections.items()}


def _safe_relative(value: object) -> Path:
    if not isinstance(value, str) or not value:
        raise RuntimeError("#8019 raw patch path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or "." in path.parts or ".." in path.parts:
        raise RuntimeError("#8019 raw patch path is unsafe")
    return Path(*path.parts)


def _regular_file(path: Path) -> Path:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("#8019 raw patch is missing or unsafe")
    return path


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["audit_luanti_8019_gate4"]
