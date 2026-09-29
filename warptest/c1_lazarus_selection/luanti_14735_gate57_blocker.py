"""Typed terminal Gate-3/4 blocker for Luanti #14735.

The PR is correctly mapped to issue #14692, but its staged test path only
changes ``<catch.hpp>`` to the new ``catch.h`` include.  The manifest records
that as ``include_only`` and terminal quality rejection, not a regression
assertion.  The raw diff also contains two support/CMake paths omitted from
the production inventory.  Finally, a production-only inverse removes the
new ``src/catch.h`` while preserving the test include, so the inverse test
cannot compile.  No capsule, build, attempt, paid action, or formal trial is
created here.
"""

from __future__ import annotations

import os
import re
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


_CANDIDATE = {"repository_key": "luanti", "change_number": 14735}
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/14735/source/v2", "candidate_source_packet", "2.0"
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/14735/screening/v2", "candidate_test_patch", "2.0"
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/14735/preannotation/v2", "source_atomicity", "2.0"
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/14735/test_manifest/v2", "candidate_stage", "2.0"
)
_BLOCKER = ArtifactReference(
    "candidate_gate57_14735_gate4_blocker/luanti/14735/native/v1",
    "candidate_gate57_14735_gate4_blocker",
    "1.0",
)

_PRODUCTION_PATHS = (
    "lib/catch2/catch.hpp",
    "lib/catch2/catch_amalgamated.cpp",
    "lib/catch2/catch_amalgamated.hpp",
    "src/benchmark/benchmark.cpp",
    "src/benchmark/benchmark_activeobjectmgr.cpp",
    "src/benchmark/benchmark_lighting.cpp",
    "src/benchmark/benchmark_mapblock.cpp",
    "src/benchmark/benchmark_mapmodify.cpp",
    "src/benchmark/benchmark_serialize.cpp",
    "src/benchmark/benchmark_setup.h",
    "src/catch.cpp",
    "src/catch.h",
    "src/unittest/test.cpp",
)
_TEST_PATHS = ("src/unittest/test_clientactiveobjectmgr.cpp",)
_UNACCOUNTED_PATHS = ("lib/catch2/CMakeLists.txt", "src/CMakeLists.txt")


def write_luanti_14735_gate57_blocker(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Replay current-v2 and persist the terminal no-execution stop."""

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#14735 blocker authorization is inactive")
    root = Path(artifact_root).absolute().resolve(strict=True)
    source, patch, atomicity, manifest = tuple(
        _load(root, ref) for ref in (_SOURCE, _PATCH, _ATOMICITY, _MANIFEST)
    )
    _validate_chain(source, patch, atomicity, manifest)
    raw_patch = _regular_file(
        root / _safe_relative(patch.payload["raw_patch_path"]), "#14735 raw patch"
    ).read_text(encoding="utf-8", errors="replace")
    sections = _patch_sections(raw_patch)
    declared = set(_PRODUCTION_PATHS + _TEST_PATHS)
    if set(sections) != declared | set(_UNACCOUNTED_PATHS):
        raise RuntimeError("#14735 raw patch inventory drifted")
    test_section = sections[_TEST_PATHS[0]]
    added_lines = [
        line[1:]
        for line in test_section.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]
    if added_lines != ['#include "catch.h"']:
        raise RuntimeError("#14735 test include-only hunk changed")
    if "#include <catch.hpp>" not in test_section:
        raise RuntimeError("#14735 old Catch include is absent from test hunk")

    required = {
        "candidate_profile": root / "artifacts/candidate_profile/luanti/14735",
        "candidate_preparation": root / "artifacts/candidate_preparation/luanti/14735",
        "fixed_source_capsule": root / "artifacts/fixed_source_capsule/luanti/14735",
        "gate4_api_check": root / "artifacts/candidate_gate4_api_check/luanti/14735",
        "gate57_attempts": root / "artifacts/candidate_gate57_attempt/luanti/14735",
    }
    present = {name: path.exists() for name, path in required.items()}
    if any(present.values()):
        raise RuntimeError("#14735 blocker inputs changed; re-audit exact chain")

    payload: dict[str, Any] = {
        "candidate": _CANDIDATE,
        "profile_id": "luanti-14735-catch2-build-v1",
        "state": "blocked",
        "outcome": "gate3_gate4_quality_blocked",
        "terminal": True,
        "resumable": False,
        "reason": "manifest_include_only_unaccounted_support_paths_fixed_only_catch_header",
        "candidate_execution_performed": False,
        "candidate_materialization_performed": False,
        "new_build_started": False,
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "current_gate_status": {
            "source_authority": "passed",
            "single_defect_mapping": "passed_primary_14692",
            "strict_inverse_patch": "blocked_unaccounted_support_paths",
            "test_api_compatibility": "blocked_fixed_only_catch_header",
            "test_manifest_quality": "terminal_include_only",
            "fresh_gate57_epoch": "blocked_before_first_attempt",
        },
        "patch_audit": {
            "declared_production_paths": list(_PRODUCTION_PATHS),
            "declared_test_paths": list(_TEST_PATHS),
            "raw_patch_paths": sorted(sections),
            "unaccounted_support_paths": list(_UNACCOUNTED_PATHS),
            "fixed_api": "src/catch.h",
            "fixed_api_added_in_production_paths": ["src/catch.h", "src/catch.cpp"],
            "preserved_test_fixed_api_reference_count": 1,
            "preserved_test_include": '#include "catch.h"',
            "production_only_inverse": True,
            "buggy_compile_expected": True,
            "explanation": (
                "The inverse removes the new src/catch.h while the preserved test "
                "still includes catch.h."
            ),
        },
        "manifest_quality": {
            "state": manifest.payload["state"],
            "outcome": manifest.payload["outcome"],
            "reason": manifest.payload.get("details", {}).get("reason"),
            "behavioral_regression_test": False,
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
        },
        "missing_for_fresh_gate5_7": [
            "current_v2_support_path_accounting_for_two_cmake_files",
            "behavioral_test_for_unittest_and_benchmark_launch",
            "candidate_specific_profile_and_fixed_source_capsule",
            "gate4_api_compatibility_receipt",
            "fresh_12_cell_gate57_runner_and_aggregate",
        ],
        "next_action": (
            "re-enumerate with support paths and a real unittest/benchmark behavior contract; do not execute this candidate"
        ),
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    inputs = (_SOURCE, _PATCH, _ATOMICITY, _MANIFEST)
    existing = _try_load(root)
    if existing is not None:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError("#14735 blocker conflicts with current-v2 state")
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
        raise RuntimeError("#14735 blocker typed reload failed")
    return artifact


def _validate_chain(
    source: StableArtifact,
    patch: StableArtifact,
    atomicity: StableArtifact,
    manifest: StableArtifact,
) -> None:
    chain = (source, patch, atomicity, manifest)
    if any(artifact.payload.get("candidate") != _CANDIDATE for artifact in chain):
        raise RuntimeError("#14735 chain candidate binding is invalid")
    if source.payload.get("complete") is not True:
        raise RuntimeError("#14735 source packet is incomplete")
    if (
        patch.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
        or tuple(patch.payload.get("production_paths", ())) != _PRODUCTION_PATHS
        or tuple(patch.payload.get("test_paths", ())) != _TEST_PATHS
        or patch.payload.get("source_manifest_path")
        != source.payload.get("raw_evidence_path")
    ):
        raise RuntimeError("#14735 patch chain is not current-v2 eligible")
    if (
        atomicity.payload.get("complete") is not True
        or atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("primary_issue_number") != 14692
        or atomicity.payload.get("authoritative_issue_numbers") != [14692]
    ):
        raise RuntimeError("#14735 atomicity is not accepted for #14692")
    if (
        manifest.payload.get("complete") is not True
        or manifest.payload.get("stage") != "test_manifest"
        or manifest.payload.get("state") != "terminal"
        or manifest.payload.get("outcome") != "quality_rejected"
        or manifest.payload.get("details", {}).get("reason") != "include_only"
    ):
        raise RuntimeError("#14735 expected manifest quality rejection is unavailable")


def _patch_sections(raw_patch: str) -> dict[str, str]:
    matches = list(re.finditer(r"^diff --git a/(\S+) b/(\S+)\s*$", raw_patch, re.M))
    return {
        match.group(2): raw_patch[
            match.start() : matches[i + 1].start() if i + 1 < len(matches) else None
        ]
        for i, match in enumerate(matches)
    }


def _safe_relative(value: Any) -> Path:
    if not isinstance(value, str):
        raise RuntimeError("#14735 evidence path is invalid")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise RuntimeError("#14735 evidence path escapes artifact root")
    return path


def _regular_file(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"{label} is not a regular file")
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
        if str(error) in {"artifact directory is missing", "artifact current head is missing"}:
            return None
        raise


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["write_luanti_14735_gate57_blocker"]
