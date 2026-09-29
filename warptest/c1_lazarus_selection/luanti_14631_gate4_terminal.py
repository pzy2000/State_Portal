"""Typed Gate-4 terminal for Luanti #14631.

The first candidate-specific native execution reached both configure/build
variants.  The fixed build passed, while the strict production-only inverse
made the preserved ``test_k_d_tree.cpp`` unable to include the PR-added
``util/k_d_tree.h``.  This is a hard Gate-4 compatibility failure, so the
candidate is terminal and no test cells, retry, paid annotation, or formal
trial may be scheduled.
"""

from __future__ import annotations

import base64
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = {"repository_key": "luanti", "change_number": 14631}
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/14631/source/v2", "candidate_source_packet", "2.0"
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/14631/screening/v2", "candidate_test_patch", "2.0"
)
_CHECKPOINT = ArtifactReference(
    "screening_checkpoint/luanti/14631/current/v2", "screening_checkpoint", "2.0"
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/14631/preannotation/v2", "source_atomicity", "2.0"
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/14631/test_manifest/v2", "candidate_stage", "2.0"
)
_CAPSULE = ArtifactReference(
    "fixed_source_capsule/luanti/14631/fixed/v2", "fixed_source_capsule", "2.0"
)
_NAMESPACE = "fresh-20260814-14631-v1"
_BUILD_REFS = tuple(
    ArtifactReference(
        f"native_regression_build/luanti/14631/{variant}/{step}/{_NAMESPACE}",
        "native_regression_build",
        "3.0",
    )
    for variant, step in (
        ("fixed", "configure"),
        ("fixed", "build"),
        ("buggy", "configure"),
        ("buggy", "build"),
    )
)
_TERMINAL = ArtifactReference(
    "candidate_gate4_terminal/luanti/14631/native/v1",
    "candidate_gate4_terminal",
    "1.0",
)


def write_luanti_14631_gate4_terminal(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Validate the persisted build evidence and write a typed hard terminal."""

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#14631 Gate-4 terminal authorization is invalid")
    root = Path(artifact_root).absolute().resolve(strict=True)
    source, patch, checkpoint, atomicity, manifest, capsule = tuple(
        _load(root, reference)
        for reference in (_SOURCE, _PATCH, _CHECKPOINT, _ATOMICITY, _MANIFEST, _CAPSULE)
    )
    _validate_chain(source, patch, checkpoint, atomicity, manifest, capsule)
    builds = tuple(_load(root, reference) for reference in _BUILD_REFS)
    _validate_builds(*builds)

    payload: dict[str, Any] = {
        "candidate": dict(_CANDIDATE),
        "state": "terminal",
        "terminal": True,
        "resumable": False,
        "outcome": "gate4_incompatible",
        "reason": "strict_production_inverse_removes_pr_added_header_required_by_preserved_fixed_test",
        "profile_id": "luanti-14631-spatial-index-v1-native-gate57-backup",
        "current_gate_status": {
            "gate_1_source_authority": "passed",
            "gate_2_real_bug_or_regression_mapping": "passed_authoritative_issue_14613",
            "gate_3_production_only_inverse": "passed_mechanical",
            "gate_4_fixed_test_buggy_api_compatibility": "failed_fixed_only_header",
            "gate_5_fixed_f2p_3x": "not_run_gate4_terminal",
            "gate_6_buggy_f2p_3x_failure": "not_run_gate4_terminal",
            "gate_7_fixed_buggy_p2p_3x": "not_run_gate4_terminal",
        },
        "gate4_audit": {
            "inverse_scope": "strict_production_only",
            "fixed_test_path": "src/unittest/test_k_d_tree.cpp",
            "fixed_test_selector": "k-d-tree",
            "fixed_test_include": "#include \"util/k_d_tree.h\"",
            "introduced_production_path": "src/util/k_d_tree.h",
            "buggy_compile_expected": False,
            "observed_failure": "fatal error: 'util/k_d_tree.h' file not found",
            "observed_failure_line": 7,
            "fixed_build_returncode": 0,
            "buggy_build_returncode": 1,
        },
        "source_evidence": {
            "primary_issue_number": 14613,
            "linked_issue_numbers": [14613, 14614, 16275],
            "production_paths_from_current_v2": [
                "irr/include/vector3d.h",
                "src/activeobjectmgr.h",
                "src/benchmark/benchmark_activeobjectmgr.cpp",
                "src/collision.cpp",
                "src/server/activeobjectmgr.cpp",
                "src/server/activeobjectmgr.h",
                "src/server/luaentity_sao.cpp",
                "src/server/player_sao.cpp",
                "src/server/player_sao.h",
                "src/server/serveractiveobject.cpp",
                "src/server/serveractiveobject.h",
                "src/serverenvironment.cpp",
                "src/serverenvironment.h",
                "src/util/k_d_tree.h",
            ],
        },
        "build_evidence": {
            "execution_namespace": _NAMESPACE,
            "fixed_configure": _BUILD_REFS[0].artifact_id,
            "fixed_build": _BUILD_REFS[1].artifact_id,
            "buggy_configure": _BUILD_REFS[2].artifact_id,
            "buggy_build": _BUILD_REFS[3].artifact_id,
        },
        "candidate_materialization_performed": True,
        "candidate_execution_performed": True,
        "new_build_started": True,
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "fresh_epoch": {
            "required_attempt_count": 12,
            "attempt_count": 0,
            "not_run": True,
            "old_attempts_referenced": False,
        },
        "aborted_retry": {
            "namespace": "fresh-20260814-14631-v2",
            "fixed_configure_started": True,
            "stopped_before_buggy_build_and_attempts": True,
            "stopped_per_gate4_terminal": True,
        },
        "next_action": "do_not_schedule_retry_capsule_build_gate57_attempts_paid_annotation_or_formal_trial",
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    inputs = (_SOURCE, _PATCH, _CHECKPOINT, _ATOMICITY, _MANIFEST, _CAPSULE, *_BUILD_REFS)
    _assert_absent(root)
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
        raise RuntimeError("#14631 Gate-4 terminal typed reload failed")
    return artifact


def _validate_chain(
    source: StableArtifact,
    patch: StableArtifact,
    checkpoint: StableArtifact,
    atomicity: StableArtifact,
    manifest: StableArtifact,
    capsule: StableArtifact,
) -> None:
    if (
        source.payload.get("candidate") != _CANDIDATE
        or source.payload.get("complete") is not True
        or source.inputs != (
            ArtifactReference("repository_enumeration/luanti/current/v2", "repository_enumeration", "2.0"),
        )
    ):
        raise RuntimeError("#14631 source chain binding is invalid")
    if (
        patch.payload.get("candidate") != _CANDIDATE
        or patch.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
        or patch.inputs != (
            ArtifactReference("repository_enumeration/luanti/current/v2", "repository_enumeration", "2.0"),
            _SOURCE,
        )
        or patch.payload.get("source_manifest_path") != source.payload.get("raw_evidence_path")
    ):
        raise RuntimeError("#14631 patch chain binding is invalid")
    if (
        checkpoint.payload.get("candidate") != _CANDIDATE
        or checkpoint.payload.get("state") != "complete"
        or checkpoint.payload.get("outcome") != "patch_eligible"
        or checkpoint.inputs != (
            ArtifactReference("repository_enumeration/luanti/current/v2", "repository_enumeration", "2.0"),
            _PATCH,
        )
    ):
        raise RuntimeError("#14631 screening checkpoint binding is invalid")
    if (
        atomicity.payload.get("candidate") != _CANDIDATE
        or atomicity.payload.get("complete") is not True
        or atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("authoritative_issue_numbers") != [14613]
        or atomicity.payload.get("primary_issue_number") != 14613
    ):
        raise RuntimeError("#14631 source atomicity binding is invalid")
    details = manifest.payload.get("details")
    if (
        manifest.payload.get("candidate") != _CANDIDATE
        or manifest.payload.get("complete") is not True
        or manifest.payload.get("stage") != "test_manifest"
        or manifest.payload.get("state") != "passed"
        or not isinstance(details, dict)
        or details.get("executable_test_paths") != [
            "src/unittest/test_k_d_tree.cpp",
            "src/unittest/test_serveractiveobjectmgr.cpp",
        ]
        or details.get("test_count") != 2
    ):
        raise RuntimeError("#14631 test manifest binding is invalid")
    if capsule.payload.get("candidate") != _CANDIDATE:
        raise RuntimeError("#14631 fixed capsule binding is invalid")


def _validate_builds(
    fixed_configure: StableArtifact,
    fixed_build: StableArtifact,
    buggy_configure: StableArtifact,
    buggy_build: StableArtifact,
) -> None:
    values = (fixed_configure, fixed_build, buggy_configure, buggy_build)
    for artifact in values:
        if artifact.payload.get("candidate") != _CANDIDATE:
            raise RuntimeError("#14631 build candidate binding is invalid")
        if artifact.payload.get("process_outcome") != "completed":
            raise RuntimeError("#14631 build process outcome is invalid")
    if (
        fixed_configure.payload.get("step") != "configure"
        or fixed_build.payload.get("step") != "build"
        or buggy_configure.payload.get("step") != "configure"
        or buggy_build.payload.get("step") != "build"
        or fixed_build.payload.get("returncode") != 0
        or buggy_build.payload.get("returncode") != 1
    ):
        raise RuntimeError("#14631 build receipt status is invalid")
    try:
        stderr = base64.b64decode(buggy_build.payload["stdout_base64"]).decode(errors="replace")
    except (KeyError, ValueError) as error:
        raise RuntimeError("#14631 buggy build output is invalid") from error
    if "fatal error: 'util/k_d_tree.h' file not found" not in stderr:
        raise RuntimeError("#14631 Gate-4 compiler failure marker is absent")


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _assert_absent(root: Path) -> None:
    try:
        _load(root, _TERMINAL)
    except RuntimeError as error:
        if str(error) in {"artifact directory is missing", "artifact current head is missing"}:
            return
        raise
    raise RuntimeError("#14631 Gate-4 terminal already exists")


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["write_luanti_14631_gate4_terminal"]
