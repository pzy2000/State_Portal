"""Typed Gate 5--7 blocker for Luanti change 15251.

The current-v2 source, test-patch, source-atomicity and test-manifest
receipts are bound to Luanti #15251.  Mechanical replay of the raw patch
finds two independent pre-execution stops:

* ``doc/lua_api.md`` is present in the raw diff but is absent from the typed
  production/test path inventory, so the production-only inverse cannot be
  called a strict inverse of the registered patch;
* the preserved test adds calls using the PR's new ``VoxelArea::diff``
  template and a ``std::vector`` result, while the production-only inverse
  restores the buggy ``std::list`` signature.  The fixed test therefore is
   not compileable against the buggy tree.

This receipt is resumable and deliberately performs no source materialization,
build, candidate execution, paid annotation, or formal trial.  A corrected
current-v2 contract must replace it and be re-audited before a fresh twelve-
cell Gate 5--7 epoch can be scheduled.
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


_CANDIDATE = {"repository_key": "luanti", "change_number": 15251}
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/15251/source/v2",
    "candidate_source_packet",
    "2.0",
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/15251/screening/v2",
    "candidate_test_patch",
    "2.0",
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/15251/preannotation/v2",
    "source_atomicity",
    "2.0",
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/15251/test_manifest/v2",
    "candidate_stage",
    "2.0",
)
_BLOCKER = ArtifactReference(
    "candidate_gate57_15251_gate4_blocker/luanti/15251/native/v1",
    "candidate_gate57_15251_gate4_blocker",
    "1.0",
)

_PRODUCTION_PATHS = (
    "src/debug.cpp",
    "src/map.cpp",
    "src/map.h",
    "src/mapblock.cpp",
    "src/mapblock.h",
    "src/mapgen/dungeongen.cpp",
    "src/script/common/c_content.cpp",
    "src/script/cpp_api/s_base.cpp",
    "src/script/lua_api/l_object.cpp",
    "src/script/lua_api/l_object.h",
    "src/script/lua_api/l_playermeta.cpp",
    "src/script/lua_api/l_playermeta.h",
    "src/script/lua_api/l_vmanip.cpp",
    "src/script/lua_api/l_vmanip.h",
    "src/threading/event.cpp",
    "src/threading/mutex_auto_lock.h",
    "src/threading/thread.cpp",
    "src/unittest/mock_serveractiveobject.h",
    "src/util/stream.h",
    "src/voxel.cpp",
    "src/voxel.h",
    "src/voxelalgorithms.cpp",
)
_TEST_PATHS = (
    "src/unittest/test_moveaction.cpp",
    "src/unittest/test_voxelarea.cpp",
)


def write_luanti_15251_gate57_blocker(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Replay exact current-v2 inputs and persist the typed pre-execution stop."""

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#15251 Gate 5--7 blocker authorization is inactive")

    root = Path(artifact_root).absolute().resolve(strict=True)
    chain = tuple(
        _load(root, reference) for reference in (_SOURCE, _PATCH, _ATOMICITY, _MANIFEST)
    )
    source, patch, atomicity, manifest = chain
    _validate_chain(source, patch, atomicity, manifest)

    raw_patch_path = _safe_relative(
        patch.payload.get("raw_patch_path"), "#15251 raw patch path"
    )
    raw_patch = _regular_file(root / raw_patch_path, "#15251 raw patch").read_bytes()
    sections = _parse_patch(raw_patch)
    declared_paths = set(_PRODUCTION_PATHS + _TEST_PATHS)
    undeclared_paths = sorted(set(sections) - declared_paths)
    missing_paths = sorted(declared_paths - set(sections))
    if not undeclared_paths:
        raise RuntimeError(
            "#15251 expected undeclared documentation path was not observed"
        )

    api_audit = _audit_fixed_only_api(sections)
    if not api_audit["incompatible"]:
        raise RuntimeError(
            "#15251 expected fixed-only VoxelArea::diff API mismatch was not observed"
        )

    # Probe only candidate-specific namespaces.  No broad artifact scan is
    # allowed, and this receipt's own namespace is intentionally not probed.
    required = {
        "candidate_profile": root / "artifacts/candidate_profile/luanti/15251",
        "candidate_preparation": root / "artifacts/candidate_preparation/luanti/15251",
        "fixed_source_capsule": root / "artifacts/fixed_source_capsule/luanti/15251",
        "native_dependency_preflight": root
        / "artifacts/candidate_stage/luanti/15251/native_dependency_preflight",
        "gate57_profile": root / "artifacts/candidate_gate57_profile/luanti/15251",
        "gate57_aggregate": root / "artifacts/candidate_gate57_aggregate/luanti/15251",
        "gate57_transition": root
        / "artifacts/candidate_gate57_transition/luanti/15251",
        "fresh_toolchain": root
        / "artifacts/candidate_gate57_fresh_toolchain/luanti/15251",
    }
    present = {name: path.exists() for name, path in required.items()}
    if any(present.values()):
        raise RuntimeError("#15251 Gate 4 blocker inputs changed; re-audit exact chain")

    chain_revisions = {
        artifact.reference.artifact_id: artifact.created_at for artifact in chain
    }
    payload: dict[str, Any] = {
        "candidate": _CANDIDATE,
        "profile_id": "luanti-15251-safety-and-other-fixes-v2",
        "state": "blocked",
        "outcome": "gate4_compatibility_blocked",
        "terminal": False,
        "resumable": True,
        "reason": (
            "raw_patch_contains_undeclared_doc_hunk_and_preserved_test_uses_vector_diff_"
            "signature_absent_after_production_inverse"
        ),
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
            "strict_inverse_patch": "blocked_undeclared_patch_path",
            "test_api_compatibility": "blocked_fixed_only_api",
            "fresh_gate57_epoch": "blocked_before_first_attempt",
        },
        "patch_audit": {
            "declared_production_paths": list(_PRODUCTION_PATHS),
            "declared_test_paths": list(_TEST_PATHS),
            "raw_patch_paths": sorted(sections),
            "undeclared_paths": undeclared_paths,
            "missing_declared_paths": missing_paths,
            "fixed_api": "VoxelArea::diff",
            "changed_signatures": api_audit["changed_signatures"],
            "fixed_test_call_count": len(api_audit["fixed_test_calls"]),
            "fixed_test_call_examples": api_audit["fixed_test_calls"][:8],
            "fixed_result_container": "std::vector<VoxelArea>",
            "buggy_compile_expected": True,
            "explanation": (
                "The production-only inverse restores diff(std::list<VoxelArea>&), "
                "but the preserved test passes std::vector<VoxelArea>; the buggy tree "
                "therefore cannot compile the fixed test."
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
                {
                    "phase": "f2p",
                    "variant": "fixed",
                    "repetitions": 3,
                    "expected": "pass",
                },
                {
                    "phase": "f2p",
                    "variant": "buggy",
                    "repetitions": 3,
                    "expected": "failure",
                },
                {
                    "phase": "p2p",
                    "variant": "fixed",
                    "repetitions": 3,
                    "expected": "pass",
                },
                {
                    "phase": "p2p",
                    "variant": "buggy",
                    "repetitions": 3,
                    "expected": "pass",
                },
            ],
        },
        "missing_for_fresh_gate5_7": [
            "corrected_current_v2_inventory_accounting_for_doc_lua_api_md",
            "current_v2_test_contract_using_buggy_visible_diff_signature",
            "candidate_specific_profile_and_selector_binding",
            "candidate_specific_fixed_source_capsule_and_production_only_buggy_inverse",
            "candidate_specific_native_toolchain_and_dependency_closure",
            "candidate_specific_fixed_buggy_build_receipts",
            "fresh_12_cell_gate57_runner_aggregate_and_transition",
        ],
        "next_action": (
            "correct_current_v2_inventory_and_test_contract_then_rerun_gate2_4_before_any_build"
        ),
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    inputs = (_SOURCE, _PATCH, _ATOMICITY, _MANIFEST)
    existing = _try_load(root)
    if existing is not None:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError("#15251 Gate 4 blocker conflicts with current-v2 state")
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
        raise RuntimeError("#15251 Gate 4 blocker typed reload failed")
    return artifact


def _validate_chain(
    source: StableArtifact,
    patch: StableArtifact,
    atomicity: StableArtifact,
    manifest: StableArtifact,
) -> None:
    chain = (source, patch, atomicity, manifest)
    if any(artifact.payload.get("candidate") != _CANDIDATE for artifact in chain):
        raise RuntimeError("#15251 current-v2 chain candidate binding is invalid")
    if source.payload.get("complete") is not True:
        raise RuntimeError("#15251 source packet is not complete")
    if (
        patch.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
        or tuple(patch.payload.get("production_paths", ())) != _PRODUCTION_PATHS
        or tuple(patch.payload.get("test_paths", ())) != _TEST_PATHS
        or patch.payload.get("source_manifest_path")
        != source.payload.get("raw_evidence_path")
    ):
        raise RuntimeError("#15251 test patch inventory is not current-v2 eligible")
    if (
        atomicity.payload.get("complete") is not True
        or atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("authoritative_issue_count") != 1
        or atomicity.payload.get("primary_issue_number") != 14435
    ):
        raise RuntimeError("#15251 source atomicity is not accepted")
    details = manifest.payload.get("details")
    if (
        manifest.payload.get("complete") is not True
        or manifest.payload.get("stage") != "test_manifest"
        or manifest.payload.get("state") != "passed"
        or not isinstance(details, dict)
        or details.get("executable_test_paths") != list(_TEST_PATHS)
        or details.get("test_count") != 2
    ):
        raise RuntimeError("#15251 test manifest is not passed")


def _audit_fixed_only_api(sections: dict[str, tuple[str, ...]]) -> dict[str, object]:
    production = tuple(line for path in _PRODUCTION_PATHS for line in sections[path])
    test = sections[_TEST_PATHS[1]]
    deleted = tuple(line[1:] for line in production if line.startswith("-"))
    added = tuple(line[1:] for line in production if line.startswith("+"))
    changed = {
        "VoxelArea::diff": {
            "buggy": "void diff(const VoxelArea &a, std::list<VoxelArea> &result)",
            "fixed": "template <typename C> void diff(const VoxelArea &a, C &result) const",
            "buggy_present_in_deleted_lines": any(
                "void diff(const VoxelArea &a, std::list<VoxelArea> &result)" in line
                for line in deleted
            ),
            "fixed_present_in_added_lines": any(
                "template <typename C>" in line for line in added
            )
            and any(
                "void diff(const VoxelArea &a, C &result) const" in line
                for line in added
            ),
        }
    }
    fixed_test_calls = [
        line[1:].strip() for line in test if line.startswith("+") and "v1.diff(" in line
    ]
    vector_result = any(
        line.startswith("+") and "std::vector<VoxelArea> res" in line for line in test
    )
    incompatible = (
        bool(fixed_test_calls)
        and vector_result
        and all(
            value["buggy_present_in_deleted_lines"]
            and value["fixed_present_in_added_lines"]
            for value in changed.values()
        )
    )
    return {
        "incompatible": incompatible,
        "changed_signatures": changed,
        "fixed_test_calls": fixed_test_calls,
    }


def _parse_patch(raw_patch: bytes) -> dict[str, tuple[str, ...]]:
    text = raw_patch.decode("utf-8", errors="surrogateescape")
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines(keepends=True):
        if line.startswith("diff --git "):
            parts = shlex.split(line.rstrip("\r\n"))
            if len(parts) != 4 or parts[:2] != ["diff", "--git"]:
                raise RuntimeError("#15251 raw patch header is invalid")
            left, right = parts[2], parts[3]
            if (
                not left.startswith("a/")
                or not right.startswith("b/")
                or left[2:] != right[2:]
            ):
                raise RuntimeError("#15251 raw patch path header is invalid")
            path = _safe_relative(left[2:], "#15251 patch path")
            if path in sections:
                raise RuntimeError("#15251 raw patch has duplicate path")
            current = path
            sections[current] = []
        if current is not None:
            sections[current].append(line)
    if not sections:
        raise RuntimeError("#15251 raw patch has no sections")
    return {path: tuple(lines) for path, lines in sections.items()}


def _safe_relative(value: object, label: str) -> str:
    if type(value) is not str or not value:
        raise RuntimeError(f"{label} is invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {".", ".."} for part in path.parts)
        or path.as_posix() != value
    ):
        raise RuntimeError(f"{label} is unsafe")
    return path.as_posix()


def _regular_file(path: Path, label: str) -> Path:
    try:
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"{label} is missing or unsafe")
    except OSError as error:
        raise RuntimeError(f"{label} is unreadable") from error
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
        return _load(root, _BLOCKER)
    except RuntimeError as error:
        if str(error) in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            return None
        raise


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["write_luanti_15251_gate57_blocker"]
