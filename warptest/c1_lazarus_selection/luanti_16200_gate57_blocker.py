"""Typed post-build blocker for the fresh Luanti #16200 Gate 5--7 epoch.

The current-v2 mechanical chain and candidate-local native inputs are closed,
but the first fresh epoch cannot reach a test cell: CMake configure succeeds
and the fixed ``luantiserver`` link exits non-zero with unresolved Lua symbols
on arm64.  This receipt is resumable and intentionally records no semantic
candidate verdict, no buggy build, and no Gate 5--7 attempt inheritance.
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


_CANDIDATE = {"repository_key": "luanti", "change_number": 16200}
_ENUMERATION = ArtifactReference(
    "repository_enumeration/luanti/current/v2", "repository_enumeration", "2.0"
)
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/16200/source/v2",
    "candidate_source_packet",
    "2.0",
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/16200/screening/v2",
    "candidate_test_patch",
    "2.0",
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/16200/preannotation/v2", "source_atomicity", "2.0"
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/16200/test_manifest/v2", "candidate_stage", "2.0"
)
_CHECKPOINT = ArtifactReference(
    "screening_checkpoint/luanti/16200/current/v2",
    "screening_checkpoint",
    "2.0",
)
_CAPSULE = ArtifactReference(
    "fixed_source_capsule/luanti/16200/fixed/v2", "fixed_source_capsule", "2.0"
)
_TOOLCHAIN = ArtifactReference(
    "candidate_gate57_fresh_toolchain/luanti/16200/cpp17/v1",
    "candidate_gate57_fresh_toolchain",
    "1.0",
)
_CONFIGURE = ArtifactReference(
    "native_regression_build/luanti/16200/fixed/configure/v1",
    "native_regression_build",
    "3.0",
)
_BUILD = ArtifactReference(
    "native_regression_build/luanti/16200/fixed/build/v1",
    "native_regression_build",
    "3.0",
)
_BLOCKER = ArtifactReference(
    "candidate_gate57_blocker/luanti/16200/native/v2",
    "candidate_gate57_blocker",
    "1.0",
)
_PROFILE_ID = "luanti-16200-rotation-conventions-v1-native-gate57"


def _now() -> str:
    return (datetime.now(UTC) + timedelta(microseconds=1)).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _require_authorization(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#16200 post-build blocker authorization is inactive")


def _decode_output(payload: dict[str, Any]) -> str:
    encoded = payload.get("stdout_base64")
    if not isinstance(encoded, str):
        raise RuntimeError("#16200 build receipt has no typed stdout")
    try:
        return base64.b64decode(encoded, validate=True).decode("utf-8", "replace")
    except (ValueError, UnicodeError) as error:
        raise RuntimeError("#16200 build receipt stdout is invalid") from error


def write_luanti_16200_gate57_blocker(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Persist and reload the immutable current-v2 native build blocker."""

    _require_authorization(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    chain = tuple(
        _load(root, reference)
        for reference in (
            _ENUMERATION,
            _SOURCE,
            _PATCH,
            _ATOMICITY,
            _MANIFEST,
            _CHECKPOINT,
            _CAPSULE,
            _TOOLCHAIN,
            _CONFIGURE,
            _BUILD,
        )
    )
    (
        enumeration,
        source,
        patch,
        atomicity,
        manifest,
        checkpoint,
        capsule,
        toolchain,
        configure,
        build,
    ) = chain
    if _CANDIDATE not in enumeration.payload.get("candidates", []):
        raise RuntimeError("#16200 is absent from current-v2 enumeration")
    if any(artifact.payload.get("candidate") != _CANDIDATE for artifact in chain[1:]):
        raise RuntimeError("#16200 blocker inputs are not candidate-bound")
    if source.payload.get("complete") is not True:
        raise RuntimeError("#16200 source packet is not complete")
    if patch.payload.get("outcome") != "patch_eligible":
        raise RuntimeError("#16200 test patch is not eligible")
    if (
        atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("primary_issue_number") != 15195
    ):
        raise RuntimeError("#16200 source atomicity is not accepted")
    if (
        manifest.payload.get("state") != "passed"
        or manifest.payload.get("details", {}).get("test_count") != 3
    ):
        raise RuntimeError("#16200 test manifest is not closed")
    if checkpoint.payload.get("state") != "complete":
        raise RuntimeError("#16200 screening checkpoint is not complete")
    if capsule.payload.get("profile_id") != _PROFILE_ID:
        raise RuntimeError("#16200 fixed capsule profile binding drifted")
    if (
        toolchain.payload.get("fresh_epoch") != "cpp17-v1"
        or toolchain.payload.get("old_attempts_loaded") is not False
        or toolchain.payload.get("paid_actions_authorized") is not False
        or toolchain.payload.get("formal_trials_authorized") is not False
    ):
        raise RuntimeError("#16200 fresh toolchain receipt is unsafe")
    configure_payload = dict(configure.payload)
    build_payload = dict(build.payload)
    if (
        configure_payload.get("step") != "configure"
        or configure_payload.get("variant") != "fixed"
        or configure_payload.get("process_outcome") != "completed"
        or configure_payload.get("returncode") != 0
    ):
        raise RuntimeError("#16200 fixed configure receipt is not successful")
    if (
        build_payload.get("step") != "build"
        or build_payload.get("variant") != "fixed"
        or build_payload.get("process_outcome") != "completed"
        or build_payload.get("returncode") != 1
    ):
        raise RuntimeError("#16200 fixed build receipt is not the observed failure")
    build_output = _decode_output(build_payload)
    required_markers = (
        "[61/295] Linking CXX static library lib/lua/src/liblua.a",
        "[295/295] Linking CXX executable",
        "luantiserver",
        "Undefined symbols for architecture arm64:",
        "ld: symbol(s) not found for architecture arm64",
        "ninja: build stopped: subcommand failed.",
    )
    if any(marker not in build_output for marker in required_markers):
        raise RuntimeError("#16200 fixed build linker evidence is incomplete")
    if "lib/lua/src/liblua.a" not in build_output.split("[295/295]", 1)[-1]:
        raise RuntimeError("#16200 final link command does not expose Lua archive")
    undefined_lines = tuple(
        line.strip()
        for line in build_output.splitlines()
        if line.strip().startswith('"_lua_')
    )
    if not undefined_lines:
        raise RuntimeError("#16200 Lua undefined-symbol evidence is absent")

    revisions = {artifact.reference.artifact_id: artifact.created_at for artifact in chain}
    payload: dict[str, Any] = {
        "candidate": _CANDIDATE,
        "profile_id": _PROFILE_ID,
        "execution_lineage": "candidate-gate57/luanti/16200/native/v1",
        "state": "blocked",
        "outcome": "infrastructure_blocked",
        "terminal": False,
        "resumable": True,
        "reason": "fixed_server_link_unresolved_lua_symbols",
        "candidate_materialization_performed": True,
        "candidate_execution_performed": False,
        "new_build_started": True,
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "current_v2_chain_revisions": revisions,
        "current_gate_status": {
            "source_authority": "passed",
            "single_defect_mapping": "passed",
            "strict_inverse_patch": "passed",
            "test_api_compatibility": "passed",
            "native_profile_registration": "passed",
            "fixed_source_capsule_registration": "passed",
            "fixed_configure": "passed",
            "fixed_build": "blocked",
            "buggy_build": "not_started",
            "fresh_gate57_epoch": "blocked_after_fixed_build_before_first_attempt",
        },
        "toolchain_evidence": {
            "reference": _TOOLCHAIN.artifact_id,
            "language_standard": "c++17",
            "old_attempts_loaded": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
        },
        "build_evidence": {
            "configure_reference": _CONFIGURE.artifact_id,
            "build_reference": _BUILD.artifact_id,
            "configure_returncode": configure_payload["returncode"],
            "build_returncode": build_payload["returncode"],
            "target": "luantiserver",
            "architecture": "arm64",
            "lua_static_archive_built": True,
            "lua_static_archive_in_final_link_argv": True,
            "lua_objects_reported_as_cxx": "Building CXX object lib/lua/src/" in build_output,
            "undefined_symbol_prefix": "_lua_",
            "undefined_symbol_examples": list(undefined_lines[:12]),
            "linker_diagnostic": "ld: symbol(s) not found for architecture arm64",
            "build_progress": "295/295",
        },
        "fresh_epoch": {
            "required_attempt_count": 12,
            "attempt_count": 0,
            "inherit_prior_attempts": False,
            "old_attempts_referenced": False,
            "cells": [
                {"phase": "f2p", "variant": "fixed", "repetitions": 3, "expected": "pass"},
                {"phase": "f2p", "variant": "buggy", "repetitions": 3, "expected": "failure"},
                {"phase": "p2p", "variant": "fixed", "repetitions": 3, "expected": "pass"},
                {"phase": "p2p", "variant": "buggy", "repetitions": 3, "expected": "pass"},
            ],
        },
        "deferred_until_link_closure": [
            "buggy_build",
            "fresh_12_cell_gate57_runner_aggregate",
            "fresh_gate57_transition",
            "paid_annotation",
            "formal_trials",
        ],
        "next_action": (
            "repair_or_register_a_candidate_specific_lua_native_link_closure_then_restart_a_new_12_cell_epoch"
        ),
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    inputs = (
        _ENUMERATION,
        _SOURCE,
        _PATCH,
        _ATOMICITY,
        _MANIFEST,
        _CHECKPOINT,
        _CAPSULE,
        _TOOLCHAIN,
        _CONFIGURE,
        _BUILD,
    )
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
        raise RuntimeError("#16200 post-build blocker typed reload failed")
    return artifact


__all__ = ["write_luanti_16200_gate57_blocker"]
