"""Candidate-specific current-v2 Gate 3/4 audit for Luanti #12445.

The preserved non-LuaJIT test calls ``lua_atccall``, an API introduced by the
production patch.  A strict production-only inverse removes that declaration
and definition while retaining the fixed test, so the discriminatory bundled
Lua configuration cannot compile.  This is a Gate 4 terminal; no build or
candidate execution belongs in this module.
"""

from __future__ import annotations

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


_CANDIDATE = {"repository_key": "luanti", "change_number": 12445}
_ENUMERATION = ArtifactReference(
    "repository_enumeration/luanti/current/v2", "repository_enumeration", "2.0"
)
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/12445/source/v2", "candidate_source_packet", "2.0"
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/12445/screening/v2", "candidate_test_patch", "2.0"
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/12445/preannotation/v2", "source_atomicity", "2.0"
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/12445/test_manifest/v2", "candidate_stage", "2.0"
)
_CHECKPOINT = ArtifactReference(
    "screening_checkpoint/luanti/12445/current/v2", "screening_checkpoint", "2.0"
)
_TERMINAL = ArtifactReference(
    "candidate_gate34_terminal/luanti/12445/current-v2/v1",
    "candidate_gate34_terminal",
    "1.0",
)

_PRODUCTION_PATHS = (
    "lib/lua/src/lapi.c",
    "lib/lua/src/ldo.c",
    "lib/lua/src/lstate.c",
    "lib/lua/src/lstate.h",
    "lib/lua/src/lua.h",
    "src/script/cpp_api/s_base.cpp",
)
_TEST_PATHS = ("src/unittest/test_lua.cpp",)
_FIXED_ONLY_API = "lua_atccall(lua_State*, lua_CFunctionwrapper)"


def audit_luanti_12445_gate34(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Replay current-v2 Gate 3/4 and persist the candidate-specific terminal."""

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "candidate-screening"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#12445 Gate 3/4 authorization is inactive")
    root = Path(artifact_root).absolute().resolve(strict=True)
    source, patch, atomicity, manifest, checkpoint = (
        _load(root, reference)
        for reference in (_SOURCE, _PATCH, _ATOMICITY, _MANIFEST, _CHECKPOINT)
    )
    _validate_chain(source, patch, atomicity, manifest, checkpoint)

    raw_patch = _regular_file(root / _safe_relative(patch.payload.get("raw_patch_path"))).read_bytes()
    sections = _parse_patch(raw_patch)
    if tuple(sections) != _PRODUCTION_PATHS + _TEST_PATHS:
        raise RuntimeError("#12445 raw patch inventory drifted")
    inverse_patch = _production_only_patch(sections)
    api = _audit_fixed_only_api(sections)
    if not api["incompatible"]:
        raise RuntimeError("#12445 expected fixed-only API mismatch was not observed")

    payload: dict[str, Any] = {
        "candidate": _CANDIDATE,
        "profile_id": "luanti-12445-puc-lua-cxx-exception-wrapper-v1",
        "state": "terminal",
        "outcome": "gate4_incompatible",
        "terminal": True,
        "resumable": False,
        "reason": "preserved_non_luajit_test_calls_lua_atccall_removed_by_production_only_inverse",
        "candidate_execution_performed": False,
        "candidate_materialization_performed": False,
        "new_build_started": False,
        "old_terminals_loaded": False,
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "current_gate_status": {
            "gate_1_source_authority": "passed",
            "gate_2_single_defect_mapping": "passed_issue_12681",
            "gate_3_production_only_inverse": "passed_mechanical",
            "gate_4_fixed_test_buggy_api_compatibility": "failed_fixed_only_api",
            "gate_5_7": "not_run_terminal",
        },
        "production_only_inverse": {
            "paths": list(_PRODUCTION_PATHS),
            "preserved_test_paths": list(_TEST_PATHS),
            "argv_contract": ["git", "apply", "-R", "--whitespace=nowarn", "-"],
            "byte_count": len(inverse_patch),
        },
        "api_audit": api,
        "fresh_epoch": {
            "required_attempt_count": 12,
            "attempt_count": 0,
            "inherit_prior_attempts": False,
        },
        "next_action": "do_not_schedule_gate5_7_for_this_fixed_test_contract",
        "complete": True,
    }
    inputs = (_SOURCE, _PATCH, _ATOMICITY, _MANIFEST, _CHECKPOINT)
    existing = _try_load(root)
    if existing is not None:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError("#12445 Gate 3/4 terminal conflicts with current-v2 state")
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
        raise RuntimeError("#12445 Gate 3/4 terminal typed reload failed")
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
        raise RuntimeError("#12445 current-v2 candidate binding drifted")
    details = manifest.payload.get("details")
    if (
        source.inputs != (_ENUMERATION,)
        or source.payload.get("complete") is not True
        or patch.inputs != (_ENUMERATION, _SOURCE)
        or patch.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
        or tuple(patch.payload.get("production_paths", ())) != _PRODUCTION_PATHS
        or tuple(patch.payload.get("test_paths", ())) != _TEST_PATHS
        or patch.payload.get("source_manifest_path") != source.payload.get("raw_evidence_path")
        or checkpoint.inputs != (_ENUMERATION, _PATCH)
        or checkpoint.payload.get("state") != "complete"
        or checkpoint.payload.get("outcome") != "patch_eligible"
        or atomicity.inputs != (_ENUMERATION, _SOURCE, _PATCH, _CHECKPOINT)
        or atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("authoritative_issue_count") != 1
        or atomicity.payload.get("primary_issue_number") != 12681
        or manifest.inputs != (_PATCH,)
        or manifest.payload.get("state") != "passed"
        or type(details) is not dict
        or details.get("executable_test_paths") != list(_TEST_PATHS)
        or details.get("test_count") != 1
    ):
        raise RuntimeError("#12445 current-v2 Gate 1-4 chain is invalid")


def _audit_fixed_only_api(sections: dict[str, tuple[str, ...]]) -> dict[str, Any]:
    production = tuple(line for path in _PRODUCTION_PATHS for line in sections[path])
    test = sections[_TEST_PATHS[0]]
    typedef_added = any(
        line.startswith("+") and "typedef int (*lua_CFunctionwrapper)" in line
        for line in production
    )
    declaration_added = any(
        line.startswith("+") and "lua_atccall" in line for line in sections["lib/lua/src/lua.h"]
    )
    definition_added = any(
        line.startswith("+") and "LUA_API lua_CFunctionwrapper lua_atccall" in line
        for line in sections["lib/lua/src/lapi.c"]
    )
    test_calls = [
        line[1:].strip()
        for line in test
        if line.startswith("+") and "lua_atccall(L, wrapper)" in line
    ]
    non_luajit_guard = any(line.startswith("+") and "#else" in line for line in test)
    incompatible = bool(
        typedef_added
        and declaration_added
        and definition_added
        and test_calls
        and non_luajit_guard
    )
    return {
        "fixed_only_api": _FIXED_ONLY_API,
        "api_typedef_added_in_production": typedef_added,
        "api_declaration_added_in_production": declaration_added,
        "api_definition_added_in_production": definition_added,
        "preserved_test_calls": test_calls,
        "discriminatory_configuration": "USE_LUAJIT=0 bundled PUC Lua",
        "buggy_compile_expected": True,
        "incompatible": incompatible,
    }


def _production_only_patch(sections: dict[str, tuple[str, ...]]) -> bytes:
    return "".join(line for path in _PRODUCTION_PATHS for line in sections[path]).encode(
        "utf-8", errors="surrogateescape"
    )


def _parse_patch(raw_patch: bytes) -> dict[str, tuple[str, ...]]:
    text = raw_patch.decode("utf-8", errors="surrogateescape")
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines(keepends=True):
        if line.startswith("diff --git "):
            parts = shlex.split(line.rstrip("\r\n"))
            if len(parts) != 4 or parts[:2] != ["diff", "--git"]:
                raise RuntimeError("#12445 raw patch header is invalid")
            left, right = parts[2], parts[3]
            if not left.startswith("a/") or not right.startswith("b/") or left[2:] != right[2:]:
                raise RuntimeError("#12445 raw patch path header is invalid")
            current = _safe_relative(left[2:])
            if current in sections:
                raise RuntimeError("#12445 raw patch has duplicate path")
            sections[current] = []
        if current is not None:
            sections[current].append(line)
    if not sections:
        raise RuntimeError("#12445 raw patch has no sections")
    return {path: tuple(lines) for path, lines in sections.items()}


def _safe_relative(value: object) -> str:
    if type(value) is not str or not value:
        raise RuntimeError("#12445 path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {".", ".."} for part in path.parts):
        raise RuntimeError("#12445 path is unsafe")
    return path.as_posix()


def _regular_file(path: Path) -> Path:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("#12445 raw patch is missing or unsafe")
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
        if str(error) in {"artifact directory is missing", "artifact current head is missing"}:
            return None
        raise


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["audit_luanti_12445_gate34"]
