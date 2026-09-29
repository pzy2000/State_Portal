"""Typed Gate 4 compatibility blocker for Luanti change 11763.

The current-v2 source, patch, atomicity, and test-manifest receipts are
replayed exactly.  The raw diff is closed by the declared production paths,
one added test path, and two non-inverse support paths (the documentation and
unit-test CMake registration).  Gate 4 nevertheless cannot be admitted: the
preserved added test directly names ``ModMetadataDatabase`` and its Files and
SQLite3 providers, while the production-only inverse removes those APIs from
the buggy tree.  No capsule, build, execution, paid annotation, formal trial,
or roster mutation is performed from this receipt.
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


_CANDIDATE = {"repository_key": "luanti", "change_number": 11763}
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/11763/source/v2", "candidate_source_packet", "2.0"
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/11763/screening/v2", "candidate_test_patch", "2.0"
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/11763/preannotation/v2", "source_atomicity", "2.0"
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/11763/test_manifest/v2", "candidate_stage", "2.0"
)
_BLOCKER = ArtifactReference(
    "candidate_gate57_11763_gate4_blocker/luanti/11763/native/v1",
    "candidate_gate57_11763_gate4_blocker",
    "1.0",
)
_PRODUCTION_PATHS = (
    "src/client/client.cpp",
    "src/client/client.h",
    "src/content/mods.cpp",
    "src/content/mods.h",
    "src/content/subgames.cpp",
    "src/database/database-dummy.cpp",
    "src/database/database-dummy.h",
    "src/database/database-files.cpp",
    "src/database/database-files.h",
    "src/database/database-sqlite3.cpp",
    "src/database/database-sqlite3.h",
    "src/database/database.h",
    "src/gamedef.h",
    "src/main.cpp",
    "src/script/lua_api/l_storage.cpp",
    "src/server.cpp",
    "src/server.h",
    "src/unittest/test.cpp",
)
_SUPPORT_PATHS = ("doc/minetest.6", "src/unittest/CMakeLists.txt")
_TEST_PATHS = ("src/unittest/test_modmetadatadatabase.cpp",)
_FIXED_APIS = (
    "ModMetadataDatabase",
    "ModMetadataDatabaseFiles",
    "ModMetadataDatabaseSQLite3",
)


def write_luanti_11763_gate57_blocker(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Replay current-v2 inputs and persist the pre-execution Gate 4 stop."""

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#11763 Gate 4 blocker authorization is inactive")
    root = Path(artifact_root).absolute().resolve(strict=True)
    source, patch, atomicity, manifest = tuple(
        _load(root, reference) for reference in (_SOURCE, _PATCH, _ATOMICITY, _MANIFEST)
    )
    _validate_chain(source, patch, atomicity, manifest)

    raw_patch_path = _safe_relative(
        patch.payload.get("raw_patch_path"), "#11763 raw patch path"
    )
    raw_patch = _regular_file(root / raw_patch_path, "#11763 raw patch").read_bytes()
    sections = _parse_patch(raw_patch)
    declared_paths = set(_PRODUCTION_PATHS + _SUPPORT_PATHS + _TEST_PATHS)
    undeclared_paths = sorted(set(sections) - declared_paths)
    missing_paths = sorted(declared_paths - set(sections))
    if undeclared_paths or missing_paths:
        raise RuntimeError(
            "#11763 current-v2 patch closure changed; expected Gate 3 inventory no longer holds"
        )
    api_audit = _audit_fixed_only_api(sections)
    if not api_audit["incompatible"]:
        raise RuntimeError("#11763 expected fixed-only ModMetadataDatabase API mismatch was not observed")

    # No candidate-specific preparation, capsule, profile, toolchain, build,
    # aggregate, or transition namespace may already exist when this blocker is
    # first written.  This prevents a stale attempt from being relabeled.
    required = {
        "candidate_profile": root / "artifacts/candidate_profile/luanti/11763",
        "candidate_preparation": root / "artifacts/candidate_preparation/luanti/11763",
        "fixed_source_capsule": root / "artifacts/fixed_source_capsule/luanti/11763",
        "native_dependency_preflight": root
        / "artifacts/candidate_stage/luanti/11763/native_dependency_preflight",
        "gate57_profile": root / "artifacts/candidate_gate57_profile/luanti/11763",
        "gate57_aggregate": root / "artifacts/candidate_gate57_aggregate/luanti/11763",
        "gate57_transition": root / "artifacts/candidate_gate57_transition/luanti/11763",
        "fresh_toolchain": root / "artifacts/candidate_gate57_fresh_toolchain/luanti/11763",
    }
    present = {name: path.exists() for name, path in required.items()}
    if any(present.values()):
        raise RuntimeError("#11763 Gate 4 blocker inputs changed; re-audit exact chain")

    chain_revisions = {artifact.reference.artifact_id: artifact.created_at for artifact in (source, patch, atomicity, manifest)}
    payload: dict[str, Any] = {
        "candidate": _CANDIDATE,
        "profile_id": "luanti-11763-mod-storage-database-v1-native-gate57",
        "state": "blocked",
        "outcome": "gate4_compatibility_blocked",
        "terminal": False,
        "resumable": True,
        "reason": "preserved_added_test_uses_production_only_mod_metadata_database_api",
        "candidate_execution_performed": False,
        "candidate_materialization_performed": False,
        "new_build_started": False,
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "current_v2_chain_revisions": chain_revisions,
        "current_gate_status": {
            "source_authority": "passed",
            "single_defect_mapping": "passed",
            "strict_inverse_patch": "passed",
            "test_api_compatibility": "blocked_fixed_only_api",
            "fresh_gate57_epoch": "blocked_before_first_attempt",
        },
        "gate2": {
            "state": "passed",
            "primary_issue": 10978,
            "authoritative_issue_count": 1,
            "linked_issue_numbers": [10978, 11937, 12167],
            "title": "Add database backends for mod_storage",
        },
        "gate3": {
            "state": "passed",
            "production_only_inverse": True,
            "declared_production_paths": list(_PRODUCTION_PATHS),
            "support_paths_excluded_from_inverse": list(_SUPPORT_PATHS),
            "test_paths_excluded_from_inverse": list(_TEST_PATHS),
            "raw_patch_paths": sorted(sections),
            "undeclared_paths": undeclared_paths,
            "missing_declared_paths": missing_paths,
        },
        "gate4": {
            "state": "blocked",
            "fixed_test_paths": list(_TEST_PATHS),
            "buggy_api_compile": "blocked_fixed_only_api",
            "fixed_api": list(_FIXED_APIS),
            "fixed_api_added_in_production_paths": api_audit["fixed_api_added_in_production_paths"],
            "preserved_test_fixed_api_reference_count": len(api_audit["fixed_test_api_references"]),
            "preserved_test_fixed_api_reference_examples": api_audit["fixed_test_api_references"][:12],
            "explanation": (
                "The production-only inverse removes the PR-added ModMetadataDatabase base "
                "class and Files/SQLite3 providers, while the preserved added test includes "
                "and instantiates those APIs; the buggy tree therefore cannot compile the "
                "fixed test contract."
            ),
        },
        "required_current_v2_chain": {
            name: {"registered": present[name], "probe": path.as_posix()}
            for name, path in required.items()
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
        "missing_for_fresh_gate5_7": [
            "candidate_specific_profile_and_selector_binding",
            "candidate_specific_fixed_source_capsule_and_production_only_buggy_inverse",
            "candidate_specific_native_toolchain_and_dependency_closure",
            "candidate_specific_fixed_buggy_build_receipts",
            "fresh_12_cell_gate57_runner_aggregate_and_transition",
        ],
        "next_action": "correct_current_v2_test_contract_or_inventory_then_rerun_gate2_4",
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    inputs = (_SOURCE, _PATCH, _ATOMICITY, _MANIFEST)
    existing = _try_load(root)
    if existing is not None:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError("#11763 Gate 4 blocker conflicts with current-v2 state")
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
    if artifact.inputs != inputs or artifact.payload != payload:
        raise RuntimeError("#11763 Gate 4 blocker typed reload failed")
    return artifact


def _validate_chain(source: StableArtifact, patch: StableArtifact, atomicity: StableArtifact, manifest: StableArtifact) -> None:
    chain = (source, patch, atomicity, manifest)
    if any(artifact.payload.get("candidate") != _CANDIDATE for artifact in chain):
        raise RuntimeError("#11763 current-v2 chain candidate binding is invalid")
    if source.payload.get("complete") is not True:
        raise RuntimeError("#11763 source packet is not complete")
    if (
        patch.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
        or tuple(patch.payload.get("production_paths", ())) != _PRODUCTION_PATHS
        or tuple(patch.payload.get("test_paths", ())) != _TEST_PATHS
        or patch.payload.get("source_manifest_path") != source.payload.get("raw_evidence_path")
    ):
        raise RuntimeError("#11763 test patch inventory is not current-v2 eligible")
    if (
        atomicity.payload.get("complete") is not True
        or atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("authoritative_issue_count") != 1
        or atomicity.payload.get("primary_issue_number") != 10978
    ):
        raise RuntimeError("#11763 source atomicity is not accepted")
    details = manifest.payload.get("details")
    if (
        manifest.payload.get("complete") is not True
        or manifest.payload.get("stage") != "test_manifest"
        or manifest.payload.get("state") != "passed"
        or manifest.payload.get("outcome") is not None
        or not isinstance(details, dict)
        or details.get("executable_test_paths") != list(_TEST_PATHS)
        or details.get("test_count") != 1
        or details.get("execution_candidate_kind") != "added_assertion"
    ):
        raise RuntimeError("#11763 test manifest is not passed")


def _audit_fixed_only_api(sections: dict[str, tuple[str, ...]]) -> dict[str, object]:
    production = tuple(line for path in _PRODUCTION_PATHS for line in sections[path])
    test = sections[_TEST_PATHS[0]]
    fixed_api_additions = sorted(
        path
        for path, lines in sections.items()
        if path in _PRODUCTION_PATHS
        and any(line.startswith("+") and any(api in line for api in _FIXED_APIS) for line in lines)
    )
    fixed_test_api_references = [
        line[1:].strip()
        for line in test
        if line.startswith("+") and any(api in line for api in _FIXED_APIS)
    ]
    production_added = any(
        line.startswith("+") and any(api in line for api in _FIXED_APIS)
        for line in production
    )
    incompatible = bool(fixed_api_additions) and production_added and bool(fixed_test_api_references)
    return {
        "incompatible": incompatible,
        "fixed_api_added_in_production_paths": fixed_api_additions,
        "fixed_test_api_references": fixed_test_api_references,
    }


def _parse_patch(raw_patch: bytes) -> dict[str, tuple[str, ...]]:
    text = raw_patch.decode("utf-8", errors="surrogateescape")
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines(keepends=True):
        if line.startswith("diff --git "):
            parts = shlex.split(line.rstrip("\r\n"))
            if len(parts) != 4 or parts[:2] != ["diff", "--git"]:
                raise RuntimeError("#11763 raw patch header is invalid")
            left, right = parts[2], parts[3]
            if not left.startswith("a/") or not right.startswith("b/") or left[2:] != right[2:]:
                raise RuntimeError("#11763 raw patch path header is invalid")
            path = _safe_relative(left[2:], "#11763 patch path")
            if path in sections:
                raise RuntimeError("#11763 raw patch has duplicate path")
            current = path
            sections[current] = []
        if current is not None:
            sections[current].append(line)
    if not sections:
        raise RuntimeError("#11763 raw patch has no sections")
    return {path: tuple(lines) for path, lines in sections.items()}


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
        if str(error) in {"artifact directory is missing", "artifact current head is missing"}:
            return None
        raise


def _regular_file(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"{label} is missing or unsafe")
    return path


def _safe_relative(value: object, label: str) -> str:
    if type(value) is not str or not value:
        raise RuntimeError(f"{label} is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {".", ".."} for part in path.parts) or path.as_posix() != value:
        raise RuntimeError(f"{label} is unsafe")
    return path.as_posix()


def _now() -> str:
    return (datetime.now(UTC) + timedelta(microseconds=1)).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["write_luanti_11763_gate57_blocker"]
