"""Bounded Gate 5--7 preflight for Luanti #14085.

The current-v2 source, patch, atomicity and test-manifest heads for #14085
are present and internally consistent.  The regression is explicitly a
Windows ``safeWriteToFile`` durability bug: the changed branch is guarded by
``_WIN32`` and the added test only becomes a fixed/buggy discriminator on a
Windows native build.  This adapter therefore records a resumable typed
blocker when the local candidate cache has no registered Windows runner,
source capsule, or platform-matching toolchain.  It never fabricates a
truth-table result, loads old attempts, starts a build, or launches a process.
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
    "candidate_source_packet/luanti/14085/source/v2", "candidate_source_packet", "2.0"
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/14085/screening/v2", "candidate_test_patch", "2.0"
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/14085/preannotation/v2", "source_atomicity", "2.0"
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/14085/test_manifest/v2", "candidate_stage", "2.0"
)
_CHECKPOINT = ArtifactReference(
    "screening_checkpoint/luanti/14085/current/v2", "screening_checkpoint", "2.0"
)
_PROFILE = ArtifactReference(
    "candidate_gate57_14085_windows_profile/luanti/14085/v1",
    "candidate_gate57_14085_windows_profile",
    "1.0",
)
_BLOCKER = ArtifactReference(
    "candidate_gate57_14085_preflight_blocker/luanti/14085/v1",
    "candidate_gate57_14085_preflight_blocker",
    "1.0",
)

_PRODUCTION_PATH = "src/filesys.cpp"
_TEST_PATH = "src/unittest/test_filesys.cpp"
_F2P_SELECTOR = "TestFileSys.testSafeWriteToFile"
_P2P_SELECTOR = "TestFileSys.testPathStartsWith"


def audit_luanti_14085_gate57(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Write and reload a non-terminal, candidate-bound Gate 5--7 blocker."""

    _require_authorization(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    enumeration, source, patch, atomicity, manifest, checkpoint = tuple(
        _load(root, reference)
        for reference in (
            _ENUMERATION,
            _SOURCE,
            _PATCH,
            _ATOMICITY,
            _MANIFEST,
            _CHECKPOINT,
        )
    )
    chain = (enumeration, source, patch, atomicity, manifest, checkpoint)
    if any(artifact.payload.get("candidate") != _CANDIDATE for artifact in chain[1:]):
        raise RuntimeError("#14085 current-v2 chain is not candidate-bound")
    if enumeration.payload.get("complete") is not True:
        raise RuntimeError("#14085 Luanti enumeration is incomplete")
    if source.payload.get("complete") is not True:
        raise RuntimeError("#14085 source packet is incomplete")
    if (
        patch.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
    ):
        raise RuntimeError("#14085 patch is not eligible")
    if (
        atomicity.payload.get("complete") is not True
        or atomicity.payload.get("outcome") != "accepted"
    ):
        raise RuntimeError("#14085 source atomicity is not accepted")
    if (
        manifest.payload.get("complete") is not True
        or manifest.payload.get("state") != "passed"
    ):
        raise RuntimeError("#14085 test manifest is not passed")
    if checkpoint.payload.get("state") != "complete":
        raise RuntimeError("#14085 screening checkpoint is incomplete")
    if tuple(patch.payload.get("production_paths", ())) != (_PRODUCTION_PATH,):
        raise RuntimeError("#14085 production path inventory drifted")
    if tuple(patch.payload.get("test_paths", ())) != (_TEST_PATH,):
        raise RuntimeError("#14085 test path inventory drifted")
    details = manifest.payload.get("details")
    if not isinstance(details, dict) or details.get("executable_test_paths") != [
        _TEST_PATH
    ]:
        raise RuntimeError("#14085 manifest test path binding drifted")

    source_manifest_path = _safe_relative(
        source.payload.get("raw_evidence_path"), "source evidence path"
    )
    patch_path = _safe_relative(
        patch.payload.get("raw_patch_path"), "patch evidence path"
    )
    source_manifest = _read_json(root / source_manifest_path)
    raw_patch = (root / patch_path).read_text(
        encoding="utf-8", errors="surrogateescape"
    )
    _assert_static_binding(source_manifest, raw_patch)

    profile_present = _artifact_present(root, _PROFILE)
    capsule_reference = ArtifactReference(
        "fixed_source_capsule/luanti/14085/fixed/v2", "fixed_source_capsule", "2.0"
    )
    capsule_present = _artifact_present(root, capsule_reference)
    materialization_reference = ArtifactReference(
        "candidate_stage/luanti/14085/version_materialization/v2",
        "candidate_stage",
        "2.0",
    )
    materialization_present = _artifact_present(root, materialization_reference)
    windows_runner_registered = False
    host_platform = platform.system()
    host_arch = platform.machine()

    payload: dict[str, Any] = {
        "candidate": _CANDIDATE,
        "state": "blocked",
        "outcome": "infrastructure_blocked",
        "terminal": False,
        "resumable": True,
        "candidate_execution_performed": False,
        "new_build_started": False,
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "does_not_invalidate_gate_1_4": True,
        "reason": "windows_only_regression_has_no_registered_windows_native_runner",
        "current_gate_status": {
            "gate_1_source_authority": "passed",
            "gate_2_single_defect_mapping": "passed",
            "gate_3_strict_inverse_patch": "passed",
            "gate_4_fixed_test_api_compatibility": "passed",
            "gate_5_fixed_f2p_3x": "not_run",
            "gate_6_buggy_f2p_3x_failure": "blocked_platform",
            "gate_7_p2p_3x_both_pass": "not_run",
        },
        "source_patch_binding": {
            "production_paths": [_PRODUCTION_PATH],
            "test_paths": [_TEST_PATH],
            "f2p_selector": _F2P_SELECTOR,
            "p2p_selector": _P2P_SELECTOR,
            "fixed_only_api": False,
            "defect_platform": "Windows",
            "raw_patch_markers": [
                "#ifdef _WIN32",
                "FlushFileBuffers",
                "testSafeWriteToFile",
            ],
        },
        "truth_table": {
            "expected": {
                "fixed": {"f2p": "pass", "p2p": "pass"},
                "buggy": {"f2p": "failure", "p2p": "pass"},
            },
            "observed": None,
            "attempt_count": 0,
            "repetitions_per_cell": 3,
        },
        "platform_preflight": {
            "host_platform": host_platform,
            "host_arch": host_arch,
            "windows_runner_registered": windows_runner_registered,
            "candidate_profile_present": profile_present,
            "fixed_source_capsule_present": capsule_present,
            "version_materialization_present": materialization_present,
            "required_runner": "registered Windows native build and test runner",
            "required_compile_branch": "_WIN32",
        },
        "required_closure": {
            "profile": _PROFILE.artifact_id,
            "fixed_source_capsule": capsule_reference.artifact_id,
            "fixed_buggy_pair": True,
            "windows_native_toolchain": True,
            "fresh_attempt_namespace": "candidate_gate57_14085_windows_attempt",
            "attempts": 12,
        },
        "next_action": "register_candidate_specific_windows_runner_and_materialize_fixed_buggy_pair_then_run_fresh_gate57_epoch",
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    inputs = (
        enumeration.reference,
        source.reference,
        patch.reference,
        atomicity.reference,
        manifest.reference,
        checkpoint.reference,
    )
    existing = _try_load(root, _BLOCKER)
    if existing is not None:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError("#14085 preflight blocker conflicts with current state")
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
        raise RuntimeError("#14085 preflight blocker typed reload failed")
    return artifact


def _assert_static_binding(source_manifest: dict[str, Any], raw_patch: str) -> None:
    pull = source_manifest.get("pull")
    if (
        not isinstance(pull, dict)
        or pull.get("number") != 14085
        or pull.get("merged") is not True
    ):
        raise RuntimeError("#14085 raw source authority binding is invalid")
    labels = [
        item.get("name") for item in pull.get("labels", []) if isinstance(item, dict)
    ]
    if "Windows" not in labels or "Bugfix 🐛" not in labels:
        raise RuntimeError(
            "#14085 Windows/bugfix classification evidence is incomplete"
        )
    for marker in ("#ifdef _WIN32", "FlushFileBuffers", "testSafeWriteToFile"):
        if marker not in raw_patch:
            raise RuntimeError(f"#14085 patch marker missing: {marker}")


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _try_load(root: Path, reference: ArtifactReference) -> StableArtifact | None:
    try:
        return _load(root, reference)
    except RuntimeError as error:
        if str(error) in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            return None
        raise


def _artifact_present(root: Path, reference: ArtifactReference) -> bool:
    return _try_load(root, reference) is not None


def _safe_relative(value: object, label: str) -> PurePosixPath:
    if type(value) is not str or not value:
        raise RuntimeError(f"{label} is invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {".", ".."} for part in path.parts)
    ):
        raise RuntimeError(f"{label} is unsafe")
    return path


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("#14085 source manifest is invalid")
    return value


def _require_authorization(value: AuthorizationRecord) -> None:
    if (
        type(value) is not AuthorizationRecord
        or value.scope != "candidate-screening"
        or value.revoked
        or value.local_user_id != os.getuid()
    ):
        raise PermissionError("#14085 Gate 5--7 preflight authorization is inactive")


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["audit_luanti_14085_gate57"]
