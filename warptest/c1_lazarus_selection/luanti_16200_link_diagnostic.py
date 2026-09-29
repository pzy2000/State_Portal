"""Candidate-specific proof for the Luanti #16200 Lua archive link blocker."""

from __future__ import annotations

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


_CAPSULE = ArtifactReference(
    "fixed_source_capsule/luanti/16200/fixed/v2", "fixed_source_capsule", "2.0"
)
_TOOLCHAIN_V1 = ArtifactReference(
    "candidate_gate57_fresh_toolchain/luanti/16200/cpp17/v1",
    "candidate_gate57_fresh_toolchain",
    "1.0",
)
_BUILD_V1 = ArtifactReference(
    "native_regression_build/luanti/16200/fixed/build/v1",
    "native_regression_build",
    "3.0",
)
_BLOCKER_V2 = ArtifactReference(
    "candidate_gate57_blocker/luanti/16200/native/v2",
    "candidate_gate57_blocker",
    "1.0",
)
_DIAGNOSTIC = ArtifactReference(
    "candidate_gate57_link_diagnostic/luanti/16200/native/v1",
    "candidate_gate57_link_diagnostic",
    "1.0",
)


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


def write_luanti_16200_link_diagnostic(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#16200 link diagnostic authorization is inactive")
    root = Path(artifact_root).absolute().resolve(strict=True)
    chain = tuple(
        _load(root, reference)
        for reference in (_CAPSULE, _TOOLCHAIN_V1, _BUILD_V1, _BLOCKER_V2)
    )
    capsule, toolchain, build, blocker = chain
    if capsule.payload.get("candidate") != _CANDIDATE or toolchain.payload.get(
        "candidate"
    ) != _CANDIDATE:
        raise RuntimeError("#16200 link diagnostic inputs are not candidate-bound")
    if build.payload.get("returncode") != 1 or build.payload.get("step") != "build":
        raise RuntimeError("#16200 v1 build receipt is not the observed link failure")
    if blocker.payload.get("outcome") != "infrastructure_blocked":
        raise RuntimeError("#16200 v2 blocker outcome drifted")

    payload: dict[str, Any] = {
        "candidate": _CANDIDATE,
        "state": "proved",
        "outcome": "source_preserving_lua_archive_repair_proved",
        "terminal": False,
        "resumable": True,
        "source_preserving": True,
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "reason": "header_inputs_are_precompiled_as_non_macho_members_before_lua_c_objects",
        "current_v2_link_evidence": {
            "build_reference": _BUILD_V1.artifact_id,
            "configure_and_link_target": "luantiserver",
            "link_order": "engine_objects_then_zlib_zstd_sqlite_jsoncpp_liblua_bitop_sha256_gmp",
            "link_order_is_primary_fault": False,
            "lua_archive_in_final_link_argv": True,
            "undefined_symbol_prefix": "_lua_",
            "architecture": "arm64",
        },
        "source_contract_evidence": {
            "path": "lib/lua/src/CMakeLists.txt",
            "header_glob_line": 2,
            "header_in_source_list_line": 5,
            "static_library_line": 38,
            "language_cxx_line": 46,
            "effect": "LUA_CORE_HDRS precedes *.c and LANGUAGE CXX is applied to headers and C files",
        },
        "v1_reproduction": {
            "header_compile_mode": "clang++ -c lapi.h -o lapi.h.o",
            "header_object_format": "non-Mach-O data (precompiled-header output)",
            "archive_size_bytes": 96,
            "archive_members": ["__.SYMDEF SORTED"],
            "archive_exported_lua_symbols": [],
            "ranlib_warning": "archive member 'lapi.h.o' not a mach-o file",
        },
        "v3_source_preserving_probe": {
            "launcher_change": "prepend -x c++ to every CXX compiler invocation",
            "source_tree_changed": False,
            "fixed_capsule_reference": _CAPSULE.artifact_id,
            "header_object_format": "Mach-O 64-bit object arm64",
            "archive_size_bytes": 325712,
            "archive_member_examples": [
                "lapi.h.o",
                "lauxlib.h.o",
                "lcode.h.o",
                "ldebug.h.o",
            ],
            "exported_symbols": [
                "_lua_atpanic",
                "_lua_checkstack",
                "_lua_newthread",
                "_lua_typename",
                "_luaL_argerror",
                "_luaL_checkany",
                "_luaL_checkinteger",
            ],
            "buggy_tree_executed": False,
            "formal_trial_executed": False,
        },
        "next_action": "register_fresh_v3_toolchain_then_run_new_fixed_buggy_12_cell_epoch",
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    inputs = (_CAPSULE, _TOOLCHAIN_V1, _BUILD_V1, _BLOCKER_V2)
    write_stable_artifact(
        root,
        artifact_id=_DIAGNOSTIC.artifact_id,
        artifact_kind=_DIAGNOSTIC.artifact_kind,
        semantic_version=_DIAGNOSTIC.semantic_version,
        created_at=_now(),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(root, _DIAGNOSTIC)
    if artifact.inputs != inputs or dict(artifact.payload) != payload:
        raise RuntimeError("#16200 link diagnostic typed reload failed")
    return artifact


_CANDIDATE = {"repository_key": "luanti", "change_number": 16200}

__all__ = ["write_luanti_16200_link_diagnostic"]
