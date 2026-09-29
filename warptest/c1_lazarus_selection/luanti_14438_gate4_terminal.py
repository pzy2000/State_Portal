"""Typed Gate 4 terminal for Luanti #14438.

The current-v2 source atomicity maps cleanly to issue #14005 and the raw patch
inventory is exact.  However, the preserved tests call ``MockServer``'s new
``createScripting`` helper from ``src/unittest/mock_server.h``.  A strict
production-only inverse removes that helper while preserving those tests, so
the buggy tree cannot compile the fixed test contract.  The test-manifest head
also rejects the additions as ``new_api_compile_only``.  This is a hard Gate 4
quality/API terminal; no capsule, build, attempt, paid action, or formal trial
is started.
"""

from __future__ import annotations

import os
import shlex
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


_CANDIDATE = {"repository_key": "luanti", "change_number": 14438}
_ENUMERATION = ArtifactReference(
    "repository_enumeration/luanti/current/v2", "repository_enumeration", "2.0"
)
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/14438/source/v2", "candidate_source_packet", "2.0"
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/14438/screening/v2", "candidate_test_patch", "2.0"
)
_CHECKPOINT = ArtifactReference(
    "screening_checkpoint/luanti/14438/current/v2", "screening_checkpoint", "2.0"
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/14438/preannotation/v2", "source_atomicity", "2.0"
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/14438/test_manifest/v2", "candidate_stage", "2.0"
)
_TERMINAL = ArtifactReference(
    "candidate_gate14_terminal/luanti/14438/preannotation/v1",
    "candidate_gate14_terminal",
    "1.0",
)

_PRODUCTION_PATHS = (
    "src/client/client.h",
    "src/client/clientenvironment.h",
    "src/client/clientmap.h",
    "src/client/game.h",
    "src/client/renderingengine.h",
    "src/client/sound.h",
    "src/collision.cpp",
    "src/collision.h",
    "src/database/database-files.cpp",
    "src/database/database-files.h",
    "src/emerge.cpp",
    "src/emerge.h",
    "src/httpfetch.cpp",
    "src/main.cpp",
    "src/map.cpp",
    "src/map.h",
    "src/mapgen/mg_decoration.cpp",
    "src/mapgen/treegen.cpp",
    "src/mapgen/treegen.h",
    "src/network/clientpackethandler.cpp",
    "src/network/connection.cpp",
    "src/network/connection.h",
    "src/network/connection_internal.h",
    "src/network/connectionthreads.cpp",
    "src/network/connectionthreads.h",
    "src/raycast.cpp",
    "src/raycast.h",
    "src/script/lua_api/l_env.cpp",
    "src/server.cpp",
    "src/server.h",
    "src/server/activeobjectmgr.cpp",
    "src/server/activeobjectmgr.h",
    "src/server/mods.cpp",
    "src/server/mods.h",
    "src/serverenvironment.cpp",
    "src/serverenvironment.h",
    "src/servermap.cpp",
    "src/servermap.h",
    "src/tool.cpp",
    "src/tool.h",
    "src/unittest/mock_server.h",
    "src/unittest/test.cpp",
    "src/util/numeric.cpp",
    "src/util/numeric.h",
    "src/util/string.cpp",
    "src/util/string.h",
)
_TEST_PATHS = (
    "src/unittest/test_connection.cpp",
    "src/unittest/test_moveaction.cpp",
    "src/unittest/test_sao.cpp",
    "src/unittest/test_serveractiveobjectmgr.cpp",
)
_SUPPORT_PATHS = ("src/CMakeLists.txt",)


def audit_luanti_14438_gate4(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Reload current-v2 evidence and write an idempotent Gate 4 terminal."""

    _require_authorization(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    chain = tuple(
        _load(root, reference)
        for reference in (
            _ENUMERATION,
            _SOURCE,
            _PATCH,
            _CHECKPOINT,
            _ATOMICITY,
            _MANIFEST,
        )
    )
    _validate_chain(chain)
    source, patch, atomicity, manifest = chain[1], chain[2], chain[4], chain[5]
    source_manifest = _read_json(
        root / _safe_relative(source.payload["raw_evidence_path"])
    )
    raw_patch = (root / _safe_relative(patch.payload["raw_patch_path"])).read_bytes()
    sections = _parse_patch(raw_patch)
    patch_audit = _audit_patch(sections)
    api_audit = _audit_fixed_only_api(sections)
    if not patch_audit["inventory_exact"] or not api_audit["incompatible"]:
        raise RuntimeError("#14438 expected Gate 4 fixed-only API evidence is absent")

    pull = source_manifest.get("pull")
    payload: dict[str, Any] = {
        "candidate": dict(_CANDIDATE),
        "state": "terminal",
        "outcome": "gate4_incompatible",
        "terminal": True,
        "resumable": False,
        "reason": "preserved_tests_call_pr_added_createScripting_removed_by_production_only_inverse",
        "primary_issue_number": atomicity.payload["primary_issue_number"],
        "authoritative_issue_count": atomicity.payload["authoritative_issue_count"],
        "source_authority": {
            "pull_number": pull.get("number") if isinstance(pull, dict) else None,
            "pull_title": pull.get("title") if isinstance(pull, dict) else None,
            "merged": pull.get("merged") if isinstance(pull, dict) else None,
            "merge_revision": pull.get("merge_commit_sha")
            if isinstance(pull, dict)
            else None,
            "authoritative_issue_numbers": list(
                atomicity.payload["authoritative_issue_numbers"]
            ),
        },
        "current_v2_chain_revisions": {
            artifact.reference.artifact_id: artifact.created_at for artifact in chain
        },
        "patch_audit": patch_audit,
        "gate4_mechanical_audit": api_audit,
        "test_manifest_audit": {
            "state": manifest.payload["state"],
            "outcome": manifest.payload["outcome"],
            "reason": manifest.payload["details"]["reason"],
            "executable_test_paths": [],
            "test_count": 0,
        },
        "current_gate_status": {
            "gate_1_source_authority": "passed",
            "gate_2_single_defect_mapping": "passed_authoritative_issue_14005",
            "gate_3_strict_inverse_patch": "passed_mechanical",
            "gate_4_fixed_test_buggy_api_compatibility": "failed_fixed_only_api",
            "gate_5_fixed_f2p_3x": "not_run_gate4_terminal",
            "gate_6_buggy_f2p_3x_failure": "not_run_gate4_terminal",
            "gate_7_fixed_buggy_p2p_3x": "not_run_gate4_terminal",
        },
        "fresh_gate57_epoch": {
            "required_attempt_count": 12,
            "attempt_count": 0,
            "not_run": True,
            "inherit_prior_attempts": False,
            "old_attempts_referenced": False,
        },
        "candidate_materialization_performed": False,
        "candidate_execution_performed": False,
        "new_build_started": False,
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "roster_modified": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "next_action": "do_not_schedule_capsule_build_or_gate57_execution_after_gate4_terminal",
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    inputs = tuple(artifact.reference for artifact in chain)
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
            raise RuntimeError("#14438 Gate 4 terminal conflicts with current evidence")
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
        raise RuntimeError("#14438 Gate 4 terminal typed reload failed")
    return result


def _validate_chain(chain: tuple[StableArtifact, ...]) -> None:
    enumeration, source, patch, checkpoint, atomicity, manifest = chain
    details = manifest.payload.get("details")
    if (
        enumeration.payload.get("repository_key") != "luanti"
        or source.inputs != (_ENUMERATION,)
        or source.payload.get("candidate") != _CANDIDATE
        or source.payload.get("complete") is not True
        or patch.inputs != (_ENUMERATION, _SOURCE)
        or patch.payload.get("candidate") != _CANDIDATE
        or patch.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
        or patch.payload.get("source_manifest_path")
        != source.payload.get("raw_evidence_path")
        or tuple(patch.payload.get("production_paths", ())) != _PRODUCTION_PATHS
        or tuple(patch.payload.get("test_paths", ())) != _TEST_PATHS
        or checkpoint.inputs != (_ENUMERATION, _PATCH)
        or checkpoint.payload.get("candidate") != _CANDIDATE
        or checkpoint.payload.get("state") != "complete"
        or checkpoint.payload.get("outcome") != "patch_eligible"
        or checkpoint.payload.get("stage_artifact_id") != _PATCH.artifact_id
        or atomicity.inputs != (_ENUMERATION, _SOURCE, _PATCH, _CHECKPOINT)
        or atomicity.payload.get("candidate") != _CANDIDATE
        or atomicity.payload.get("complete") is not True
        or atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("authoritative_issue_numbers") != [14005]
        or atomicity.payload.get("primary_issue_number") != 14005
        or manifest.inputs != (_PATCH,)
        or manifest.payload.get("candidate") != _CANDIDATE
        or manifest.payload.get("complete") is not True
        or manifest.payload.get("stage") != "test_manifest"
        or manifest.payload.get("state") != "terminal"
        or manifest.payload.get("outcome") != "quality_rejected"
        or not isinstance(details, dict)
        or details.get("reason") != "new_api_compile_only"
    ):
        raise RuntimeError("#14438 current-v2 Gate 1--4 chain drifted")


def _parse_patch(raw: bytes) -> dict[str, tuple[str, ...]]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in raw.decode("utf-8", errors="surrogateescape").splitlines(keepends=True):
        if line.startswith("diff --git "):
            parts = shlex.split(line.rstrip("\r\n"))
            if len(parts) != 4 or parts[:2] != ["diff", "--git"]:
                raise RuntimeError("#14438 raw patch header is invalid")
            left, right = parts[2], parts[3]
            if (
                not left.startswith("a/")
                or not right.startswith("b/")
                or left[2:] != right[2:]
            ):
                raise RuntimeError("#14438 raw patch path is invalid")
            current = _safe_relative(left[2:]).as_posix()
            if current in sections:
                raise RuntimeError("#14438 raw patch has duplicate path")
            sections[current] = []
        if current is not None:
            sections[current].append(line)
    return {path: tuple(lines) for path, lines in sections.items()}


def _audit_patch(sections: dict[str, tuple[str, ...]]) -> dict[str, Any]:
    raw_paths = tuple(sorted(sections))
    expected = set(_PRODUCTION_PATHS + _TEST_PATHS + _SUPPORT_PATHS)
    return {
        "inventory_exact": set(raw_paths) == expected,
        "raw_patch_paths": list(raw_paths),
        "declared_production_paths": list(_PRODUCTION_PATHS),
        "declared_test_paths": list(_TEST_PATHS),
        "support_paths_excluded_from_inverse": list(_SUPPORT_PATHS),
        "undeclared_paths": sorted(set(raw_paths) - expected),
        "strict_inverse_scope": "production_only_with_support_paths_excluded",
    }


def _audit_fixed_only_api(sections: dict[str, tuple[str, ...]]) -> dict[str, Any]:
    production = sections["src/unittest/mock_server.h"]
    test_move = sections["src/unittest/test_moveaction.cpp"]
    test_sao = sections["src/unittest/test_sao.cpp"]
    helper_added = tuple(
        line.strip()
        for line in production
        if line.startswith("+")
        and not line.startswith("+++")
        and ("createScripting" in line or "ServerScripting" in line)
    )
    preserved_calls = tuple(
        line.strip()
        for lines in (test_move, test_sao)
        for line in lines
        if line.startswith("+")
        and not line.startswith("+++")
        and "server.createScripting()" in line
    )
    added_test_lines = tuple(
        line.strip()
        for path in _TEST_PATHS
        for line in sections[path]
        if line.startswith("+") and not line.startswith("+++")
    )
    assertion_tokens = ("UASSERT", "ASSERT", "EXPECT_", "CHECK(")
    added_assertions = tuple(
        line
        for line in added_test_lines
        if any(token in line for token in assertion_tokens)
    )
    return {
        "introduced_production_api": list(helper_added),
        "preserved_test_calls": list(preserved_calls),
        "fixed_only_api": bool(helper_added and preserved_calls),
        "buggy_compile_expected": False,
        "added_test_assertions": list(added_assertions),
        "test_manifest_compile_only": not bool(added_assertions),
        "incompatible": bool(helper_added and preserved_calls),
        "explanation": (
            "The production-only inverse removes MockServer::createScripting, "
            "while preserved test_moveaction.cpp and test_sao.cpp call it."
        ),
    }


def _safe_relative(value: object) -> PurePosixPath:
    if type(value) is not str or not value:
        raise RuntimeError("#14438 path is invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {".", ".."} for part in path.parts)
        or path.as_posix() != value
    ):
        raise RuntimeError("#14438 path is unsafe")
    return path


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("#14438 source manifest is missing or unsafe")
    value = __import__("json").loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("candidate") != _CANDIDATE:
        raise RuntimeError("#14438 source manifest candidate binding drifted")
    return value


def _require_authorization(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#14438 Gate 4 terminal authorization is inactive")


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["audit_luanti_14438_gate4"]
