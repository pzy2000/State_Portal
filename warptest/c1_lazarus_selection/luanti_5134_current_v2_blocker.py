"""Typed nonterminal audit for the current-v2 Luanti #5134 candidate.

The current-v2 source packet and patch are complete, but their atomicity
receipt records two authoritative issues (3786 and 4911).  That prevents a
strict single-defect Gate 2 mapping, so a fresh Gate 5--7 epoch is not
admissible yet.  This module records the exact reason and the missing local
closure without reading or inheriting any old execution result.

This is deliberately an audit-only operation.  It does not fetch source,
materialize a tree, invoke CMake, create an attempt, or run a test.
"""

from __future__ import annotations

import json
import os
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


_CANDIDATE = {"repository_key": "luanti", "change_number": 5134}
_ENUMERATION = ArtifactReference(
    "repository_enumeration/luanti/current/v2",
    "repository_enumeration",
    "2.0",
)
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/5134/source/v2",
    "candidate_source_packet",
    "2.0",
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/5134/screening/v2",
    "candidate_test_patch",
    "2.0",
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/5134/preannotation/v2",
    "source_atomicity",
    "2.0",
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/5134/test_manifest/v2",
    "candidate_stage",
    "2.0",
)
_SCREENING_CHECKPOINT = ArtifactReference(
    "screening_checkpoint/luanti/5134/current/v2",
    "screening_checkpoint",
    "2.0",
)
_PREANNOTATION_CHECKPOINT = ArtifactReference(
    "preannotation_checkpoint/luanti/5134/current/v2",
    "preannotation_checkpoint",
    "2.0",
)
_CHAIN = (
    _ENUMERATION,
    _SOURCE,
    _PATCH,
    _SCREENING_CHECKPOINT,
    _ATOMICITY,
    _MANIFEST,
    _PREANNOTATION_CHECKPOINT,
)
_BLOCKER = ArtifactReference(
    "candidate_gate57_5134_current_v2_blocker/luanti/5134/native/v1",
    "candidate_gate57_5134_current_v2_blocker",
    "1.0",
)

_PRODUCTION_PATHS = (
    "src/threading/mutex.cpp",
    "src/threading/mutex.h",
    "src/threading/thread.cpp",
    "src/threading/thread.h",
)
_TEST_PATHS = ("src/unittest/test_threading.cpp",)
_PATCH_MARKERS = (
    "try_lock",
    "m_start_finished_mutex",
    "TEST(testStartStopWait)",
    "USE_WIN_MUTEX",
    "pthread_mutex_trylock",
)
_F2P_SELECTOR = "TestThreading.testStartStopWait"
_F2P_ARGV = (
    "./bin/minetestserver",
    "--run-unittests",
    "--test-module",
    "TestThreading",
)
_FIXED_CAPSULE = PurePosixPath("private/fixed-capsules/luanti/5134/fixed-source")
_IRRLICHT_ROOT = PurePosixPath("private/dependencies/luanti-5134/irrlicht-1.8.4")
_IRRLICHT_VERSION = "1.8.4"
_IRRLICHT_FILES = (
    PurePosixPath("include/irrlicht.h"),
    PurePosixPath("include/IrrlichtVersion.h"),
    PurePosixPath("lib/Linux/libIrrlicht.a"),
)


def write_luanti_5134_current_v2_blocker(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Persist a candidate-specific, resumable Gate 5--7 blocker.

    Only the current-v2 typed chain and exact raw source/patch evidence are
    read.  A source atomicity rejection is a nonterminal screening state, not
    a candidate-elimination conclusion; the receipt therefore leaves the
    candidate resumable after the authority mapping is repaired.
    """

    _require_authorization(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    chain = tuple(_load(root, reference) for reference in _CHAIN)
    _validate_chain(chain)
    source, patch, _screening, atomicity, manifest = chain[1:6]

    source_manifest_path = _relative_payload_path(
        source.payload, "raw_evidence_path", "#5134 source packet"
    )
    patch_path = _relative_payload_path(
        patch.payload, "raw_patch_path", "#5134 patch receipt"
    )
    declared_manifest_path = _relative_payload_path(
        atomicity.payload, "source_manifest_path", "#5134 atomicity receipt"
    )
    if declared_manifest_path != source_manifest_path:
        raise RuntimeError("#5134 source manifest binding drifted")
    patch_manifest_path = _relative_payload_path(
        patch.payload, "source_manifest_path", "#5134 patch receipt"
    )
    if patch_manifest_path != source_manifest_path:
        raise RuntimeError("#5134 patch/source manifest binding drifted")
    stage_manifest_path = manifest.payload.get("source_manifest_path")
    if stage_manifest_path is not None and stage_manifest_path != source_manifest_path:
        raise RuntimeError("#5134 test manifest source binding drifted")

    raw_manifest = _read_json_file(root, source_manifest_path)
    manifest_audit = _audit_source_manifest(raw_manifest)
    patch_audit = _audit_patch(_read_bytes(root, patch_path))
    observed_assets = _probe_assets(root)

    payload = {
        "candidate": dict(_CANDIDATE),
        "profile_id": "luanti-5134-legacy-server-v1",
        "state": "blocked",
        "outcome": "infrastructure_blocked",
        "terminal": False,
        "resumable": True,
        "candidate_eligibility": "not_admitted_pending_single_defect_mapping",
        "candidate_execution_performed": False,
        "candidate_materialization_performed": False,
        "new_build_started": False,
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "does_not_freeze_or_eliminate_candidate": True,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "downstream_8_13_authorized": False,
        "reason": "current_v2_source_atomicity_rejected_multiple_authoritative_issues",
        "current_gate_status": {
            "gate_1_source_authority": "passed",
            "gate_2_single_defect_mapping": "blocked_multiple_authoritative_issues",
            "gate_3_strict_inverse_patch": "not_admissible_before_gate_2_repair",
            "gate_4_fixed_test_api_compatibility": "not_admissible_before_gate_2_repair",
            "gate_5_fixed_f2p_3x": "not_run",
            "gate_6_buggy_f2p_3x_failure": "not_run",
            "gate_7_p2p_3x_both_pass": "not_run",
        },
        "source_atomicity": {
            "receipt_outcome": atomicity.payload["outcome"],
            "reason": atomicity.payload["reason"],
            "authoritative_issue_numbers": list(
                atomicity.payload["authoritative_issue_numbers"]
            ),
            "linked_issue_numbers": list(atomicity.payload["linked_issue_numbers"]),
            "single_defect_mapping_admissible": False,
        },
        "source_patch_binding": {
            "source_manifest_path": source_manifest_path,
            "raw_patch_path": patch_path,
            "production_paths": list(_PRODUCTION_PATHS),
            "test_paths": list(_TEST_PATHS),
            "f2p_selector": _F2P_SELECTOR,
            "f2p_argv": list(_F2P_ARGV),
            "p2p_selector": "not_registered",
            "strict_inverse_patch": "not_admissible_before_gate_2_repair",
            "patch_audit": patch_audit,
        },
        "authoritative_source_audit": manifest_audit,
        "exact_closure": {
            "fixed_source_capsule_path": _FIXED_CAPSULE.as_posix(),
            "irrlicht_root": _IRRLICHT_ROOT.as_posix(),
            "required_irrlicht_version": _IRRLICHT_VERSION,
            "required_irrlicht_files": [path.as_posix() for path in _IRRLICHT_FILES],
            "fixed_source_capsule_present": observed_assets["fixed_source_capsule"][
                "present"
            ],
            "irrlicht_closure_present": observed_assets["irrlicht_closure"]["complete"],
            "closure_admissible": False,
            "candidate_specific_materialization_available": False,
        },
        "historical_plan": {
            "module": "warptest.c1_lazarus_selection.luanti_5134",
            "profile_id": "luanti-5134-legacy-server-v1",
            "read_only_reference": True,
            "used_for_new_execution": False,
            "registered_mirror_path": "private/repository-enumeration-git/luanti/mirror.git",
            "registered_dependency_path": _IRRLICHT_ROOT.as_posix(),
        },
        "observed_assets": observed_assets,
        "fresh_gate57_epoch": {
            "required_attempt_count": 12,
            "attempt_count": 0,
            "inherit_prior_attempts": False,
            "old_attempts_referenced": False,
            "candidate_specific_runner_created": False,
        },
        "next_action": (
            "repair_current_v2_single_authority_mapping_then_register_fresh_fixed_capsule_and_exact_irrlicht_1_8_4_closure_before_gate57"
        ),
        "resource_policy": {
            "network": False,
            "paid": False,
            "formal_trials": False,
            "downstream_8_13": False,
        },
    }

    inputs = tuple(_CHAIN)
    existing = _try_load(root)
    if existing is not None:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError("#5134 current-v2 blocker conflicts with exact state")
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
        raise RuntimeError("#5134 current-v2 blocker typed reload failed")
    return artifact


def _require_authorization(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "infrastructure-audit"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#5134 current-v2 audit authorization is inactive")


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


def _validate_chain(chain: tuple[StableArtifact, ...]) -> None:
    if tuple(artifact.reference for artifact in chain) != _CHAIN:
        raise RuntimeError("#5134 current-v2 chain identity drifted")
    if any(artifact.payload.get("candidate") != _CANDIDATE for artifact in chain[1:]):
        raise RuntimeError("#5134 current-v2 chain candidate binding is invalid")
    (
        enumeration,
        source,
        patch,
        screening_checkpoint,
        atomicity,
        manifest,
        checkpoint,
    ) = chain
    if enumeration.payload.get("repository_key") != "luanti":
        raise RuntimeError("#5134 enumeration is not Luanti-bound")
    if enumeration.payload.get("complete") is not True:
        raise RuntimeError("#5134 enumeration is not complete")
    if source.inputs != (_ENUMERATION,) or source.payload.get("complete") is not True:
        raise RuntimeError("#5134 source packet is not bound to current enumeration")
    if patch.inputs != (_ENUMERATION, _SOURCE):
        raise RuntimeError("#5134 patch is not bound to current source packet")
    if (
        patch.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
    ):
        raise RuntimeError("#5134 test patch is not eligible")
    if screening_checkpoint.inputs != (_ENUMERATION, _PATCH):
        raise RuntimeError("#5134 screening checkpoint is not bound to current patch")
    if (
        screening_checkpoint.payload.get("candidate") != _CANDIDATE
        or screening_checkpoint.payload.get("state") != "complete"
        or screening_checkpoint.payload.get("outcome") != "patch_eligible"
    ):
        raise RuntimeError("#5134 screening checkpoint is not complete")
    if atomicity.inputs != (_ENUMERATION, _SOURCE, _PATCH, _SCREENING_CHECKPOINT):
        raise RuntimeError("#5134 atomicity receipt is not bound to current chain")
    if (
        atomicity.payload.get("complete") is not True
        or atomicity.payload.get("outcome") != "rejected"
        or atomicity.payload.get("reason") != "multiple_authoritative_issues"
        or atomicity.payload.get("authoritative_issue_numbers") != [3786, 4911]
        or atomicity.payload.get("linked_issue_numbers") != [3786, 4911]
    ):
        raise RuntimeError("#5134 source atomicity state changed; re-audit required")
    if manifest.inputs != (_PATCH,):
        raise RuntimeError("#5134 test manifest is not bound to current patch")
    if (
        manifest.payload.get("complete") is not True
        or manifest.payload.get("state") != "passed"
    ):
        raise RuntimeError("#5134 test manifest is not passed")
    if checkpoint.inputs != (_MANIFEST,):
        raise RuntimeError("#5134 checkpoint is not bound to current manifest")
    if (
        checkpoint.payload.get("candidate") != _CANDIDATE
        or checkpoint.payload.get("state") != "active"
        or checkpoint.payload.get("current_artifact_id") != _MANIFEST.artifact_id
    ):
        raise RuntimeError("#5134 preannotation checkpoint is not current")


def _relative_payload_path(payload: dict[str, Any], key: str, label: str) -> str:
    value = payload.get(key)
    if type(value) is not str or not value:
        raise RuntimeError(f"{label} has no relative evidence path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        raise RuntimeError(f"{label} evidence path is unsafe")
    return value


def _read_bytes(root: Path, relative: str) -> bytes:
    path = _safe_raw_path(root, relative)
    try:
        info = path.lstat()
    except FileNotFoundError as error:
        raise RuntimeError("#5134 raw evidence file is missing") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError("#5134 raw evidence file is unsafe")
    return path.read_bytes()


def _read_json_file(root: Path, relative: str) -> dict[str, Any]:
    content = _read_bytes(root, relative)

    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise RuntimeError("#5134 raw manifest contains duplicate keys")
            result[key] = value
        return result

    try:
        value = json.loads(content.decode("utf-8"), object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("#5134 raw source manifest is invalid") from error
    if type(value) is not dict:
        raise RuntimeError("#5134 raw source manifest is not an object")
    return value


def _safe_raw_path(root: Path, relative: str) -> Path:
    path = root.joinpath(*PurePosixPath(relative).parts)
    current = root
    for part in PurePosixPath(relative).parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return path
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError("#5134 raw evidence path contains a symlink")
    return path


def _audit_source_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    if (
        manifest.get("candidate") != _CANDIDATE
        or manifest.get("complete") is not True
        or manifest.get("provider") != "github"
        or manifest.get("issue_numbers") != [3786, 4911]
    ):
        raise RuntimeError("#5134 raw source manifest binding is invalid")
    authority = manifest.get("authority")
    if (
        type(authority) is not dict
        or authority.get("repository") != "luanti-org/luanti"
    ):
        raise RuntimeError("#5134 raw source manifest authority is invalid")
    pull = manifest.get("pull")
    if (
        type(pull) is not dict
        or pull.get("number") != 5134
        or pull.get("merged") is not True
    ):
        raise RuntimeError("#5134 raw pull binding is invalid")
    return {
        "provider": "github",
        "repository": "luanti-org/luanti",
        "pull_number": 5134,
        "merged_pull": True,
        "issue_numbers": [3786, 4911],
        "complete": True,
    }


def _audit_patch(content: bytes) -> dict[str, Any]:
    if not content:
        raise RuntimeError("#5134 raw patch is empty")
    text = content.decode("utf-8", errors="surrogateescape")
    paths: list[str] = []
    for line in text.splitlines():
        if not line.startswith("diff --git "):
            continue
        parts = line.split(" ")
        if (
            len(parts) != 4
            or not parts[2].startswith("a/")
            or not parts[3].startswith("b/")
            or parts[2][2:] != parts[3][2:]
        ):
            raise RuntimeError("#5134 raw patch header is invalid")
        paths.append(parts[2][2:])
    expected = list(_PRODUCTION_PATHS + _TEST_PATHS)
    if paths != expected or len(set(paths)) != len(paths):
        raise RuntimeError("#5134 raw patch path inventory drifted")
    missing_markers = [marker for marker in _PATCH_MARKERS if marker not in text]
    if missing_markers:
        raise RuntimeError("#5134 raw patch markers are incomplete")
    return {
        "section_count": len(paths),
        "production_paths": list(_PRODUCTION_PATHS),
        "test_paths": list(_TEST_PATHS),
        "required_markers_present": list(_PATCH_MARKERS),
        "production_only_reverse_scope": list(_PRODUCTION_PATHS),
        "test_hunk_preserved": True,
    }


def _probe_assets(root: Path) -> dict[str, Any]:
    capsule = _probe_path(root, _FIXED_CAPSULE)
    irrlicht_root = _probe_path(root, _IRRLICHT_ROOT)
    irrlicht_files = {
        path.as_posix(): _probe_path(root, _IRRLICHT_ROOT / path)
        for path in _IRRLICHT_FILES
    }
    closure_complete = (
        irrlicht_root["present"]
        and irrlicht_root["kind"] == "directory"
        and all(
            item["present"] and item["kind"] == "file"
            for item in irrlicht_files.values()
        )
    )
    exact_namespaces = {
        "candidate_profile": _probe_path(
            root, PurePosixPath("artifacts/candidate_profile/luanti/5134")
        ),
        "candidate_gate57_profile": _probe_path(
            root, PurePosixPath("artifacts/candidate_gate57_profile/luanti/5134")
        ),
        "native_regression_aggregate": _probe_path(
            root, PurePosixPath("artifacts/native_regression_aggregate/luanti/5134")
        ),
        "native_regression_attempt": _probe_path(
            root, PurePosixPath("artifacts/native_regression_attempt/luanti/5134")
        ),
    }
    return {
        "fixed_source_capsule": capsule,
        "irrlicht_root": irrlicht_root,
        "irrlicht_files": irrlicht_files,
        "irrlicht_closure": {"complete": closure_complete},
        "exact_prior_execution_namespaces": exact_namespaces,
        "old_execution_refs_present": any(
            value["present"] for value in exact_namespaces.values()
        ),
    }


def _probe_path(root: Path, relative: PurePosixPath) -> dict[str, Any]:
    path = root.joinpath(*relative.parts)
    current = root
    try:
        for part in relative.parts:
            current = current / part
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode):
                return {"present": True, "kind": "symlink", "safe": False}
    except FileNotFoundError:
        return {"present": False, "kind": "missing", "safe": True}
    info = path.lstat()
    if stat.S_ISDIR(info.st_mode):
        kind = "directory"
    elif stat.S_ISREG(info.st_mode):
        kind = "file"
    else:
        kind = "other"
    return {"present": True, "kind": kind, "safe": kind in {"directory", "file"}}


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["write_luanti_5134_current_v2_blocker"]
