"""Typed platform/Gate-4 blocker for Luanti #7353.

The current-v2 source and patch are mechanically authoritative for issue
#3106, but the staged test is only a compile-site rewrite and the preserved
test calls the new ``porting::mt_snprintf`` API.  A production-only inverse
therefore leaves a fixed-only API in the test tree.  In addition, the defect
is MSVC-specific: Darwin takes the ordinary ``vsnprintf`` branch and the
staged test has no positional format string, so a POSIX run would be a
false-negative.  This receipt records the stop without creating a capsule,
Windows dependency closure, build, or test attempt.
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


_CANDIDATE = {"repository_key": "luanti", "change_number": 7353}
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/7353/source/v2", "candidate_source_packet", "2.0"
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/7353/screening/v2", "candidate_test_patch", "2.0"
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/7353/preannotation/v2", "source_atomicity", "2.0"
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/7353/test_manifest/v2", "candidate_stage", "2.0"
)
_BLOCKER = ArtifactReference(
    "candidate_gate57_7353_gate4_platform_blocker/luanti/7353/native/v1",
    "candidate_gate57_7353_gate4_platform_blocker",
    "1.0",
)

_PRODUCTION_PATHS = (
    "src/game.cpp",
    "src/gui/guiConfirmRegistration.cpp",
    "src/gui/profilergraph.cpp",
    "src/guiscalingfilter.cpp",
    "src/map.cpp",
    "src/mapblock.cpp",
    "src/porting.cpp",
    "src/porting.h",
    "src/script/common/c_internal.cpp",
    "src/script/cpp_api/s_base.cpp",
    "src/serverenvironment.cpp",
    "src/unittest/test.cpp",
)
_TEST_PATHS = ("src/unittest/test_connection.cpp",)


def write_luanti_7353_gate57_blocker(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Replay current-v2 inputs and persist the platform/Gate-4 stop."""

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#7353 blocker authorization is inactive")
    root = Path(artifact_root).absolute().resolve(strict=True)
    source, patch, atomicity, manifest = tuple(
        _load(root, ref) for ref in (_SOURCE, _PATCH, _ATOMICITY, _MANIFEST)
    )
    _validate_chain(source, patch, atomicity, manifest)
    raw_patch = _regular_file(
        root / _safe_relative(patch.payload["raw_patch_path"]), "#7353 raw patch"
    ).read_text(encoding="utf-8", errors="replace")
    sections = _patch_sections(raw_patch)
    if set(sections) != set(_PRODUCTION_PATHS + _TEST_PATHS):
        raise RuntimeError("#7353 raw patch path inventory drifted")
    test_section = sections[_TEST_PATHS[0]]
    fixed_test_refs = re.findall(r"porting::mt_snprintf", test_section)
    if len(fixed_test_refs) != 2 or '#include "porting.h"' not in test_section:
        raise RuntimeError("#7353 preserved test API audit is not reproducible")
    porting_section = sections["src/porting.cpp"]
    if "#ifndef _MSC_VER" not in porting_section or "_vsprintf_p" not in porting_section:
        raise RuntimeError("#7353 MSVC/non-MSVC branch audit is not reproducible")
    if re.search(r"%[0-9]+\$", test_section):
        raise RuntimeError("#7353 staged test unexpectedly exercises positional formatting")

    required = {
        "candidate_profile": root / "artifacts/candidate_profile/luanti/7353",
        "candidate_preparation": root / "artifacts/candidate_preparation/luanti/7353",
        "fixed_source_capsule": root / "artifacts/fixed_source_capsule/luanti/7353",
        "candidate_dependency_closure": root
        / "artifacts/candidate_dependency_closure/luanti/7353",
        "windows_toolchain": root / "private/dependencies/luanti-windows/7353",
        "gate57_attempts": root / "artifacts/candidate_gate57_attempt/luanti/7353",
    }
    present = {name: path.exists() for name, path in required.items()}
    if any(present.values()):
        raise RuntimeError("#7353 blocker inputs changed; re-audit exact chain")

    payload: dict[str, Any] = {
        "candidate": _CANDIDATE,
        "profile_id": "luanti-7353-msvc-snprintf-v1",
        "state": "blocked",
        "outcome": "platform_gate_blocked",
        "terminal": False,
        "resumable": True,
        "reason": "darwin_false_negative_and_preserved_test_fixed_only_api",
        "candidate_execution_performed": False,
        "candidate_materialization_performed": False,
        "new_build_started": False,
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "current_gate_status": {
            "source_authority": "passed",
            "single_defect_mapping": "passed_primary_3106",
            "strict_inverse_patch": "passed_static_production_only",
            "test_api_compatibility": "blocked_fixed_only_mt_snprintf",
            "platform_admission": "blocked_no_windows_closure",
            "fresh_gate57_epoch": "blocked_before_first_attempt",
        },
        "patch_audit": {
            "production_paths": list(_PRODUCTION_PATHS),
            "test_paths": list(_TEST_PATHS),
            "fixed_api": "porting::mt_snprintf",
            "fixed_api_added_in_production_paths": [
                "src/porting.cpp",
                "src/porting.h",
            ],
            "preserved_test_api_reference_count": len(fixed_test_refs),
            "production_only_inverse": True,
            "buggy_compile_expected": True,
            "explanation": (
                "The inverse removes mt_snprintf from porting.cpp/.h while the "
                "preserved test still includes porting.h and calls it twice."
            ),
        },
        "platform_audit": {
            "defect_scope": "MSVC positional printf support",
            "non_msvc_branch": "vsnprintf",
            "msvc_branch": "_vsprintf_p plus _vscprintf_p",
            "staged_test_has_positional_format": False,
            "darwin_result_would_be_false_negative": True,
            "windows_closure_registered": False,
            "windows_toolchain_probe": str(required["windows_toolchain"]),
        },
        "manifest_quality": {
            "state": manifest.payload["state"],
            "outcome": manifest.payload["outcome"],
            "reason": manifest.payload.get("details", {}).get("reason"),
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
            "test_contract_with_a_positional_format_assertion",
            "candidate_specific_fixed_source_capsule",
            "candidate_specific_windows_toolchain_and_dependency_closure",
            "fixed_buggy_windows_build_receipts",
            "fresh_12_cell_windows_gate57_runner_and_aggregate",
        ],
        "next_action": (
            "correct the test contract and register a Windows closure, then re-audit Gate3/4; do not run Darwin as proxy"
        ),
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    inputs = (_SOURCE, _PATCH, _ATOMICITY, _MANIFEST)
    existing = _try_load(root)
    if existing is not None:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError("#7353 blocker conflicts with current-v2 state")
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
        raise RuntimeError("#7353 blocker typed reload failed")
    return artifact


def _validate_chain(
    source: StableArtifact,
    patch: StableArtifact,
    atomicity: StableArtifact,
    manifest: StableArtifact,
) -> None:
    chain = (source, patch, atomicity, manifest)
    if any(artifact.payload.get("candidate") != _CANDIDATE for artifact in chain):
        raise RuntimeError("#7353 chain candidate binding is invalid")
    if source.payload.get("complete") is not True:
        raise RuntimeError("#7353 source packet is incomplete")
    if (
        patch.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
        or tuple(patch.payload.get("production_paths", ())) != _PRODUCTION_PATHS
        or tuple(patch.payload.get("test_paths", ())) != _TEST_PATHS
        or patch.payload.get("source_manifest_path")
        != source.payload.get("raw_evidence_path")
    ):
        raise RuntimeError("#7353 patch chain is not current-v2 eligible")
    if (
        atomicity.payload.get("complete") is not True
        or atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("primary_issue_number") != 3106
        or atomicity.payload.get("authoritative_issue_numbers") != [3106]
    ):
        raise RuntimeError("#7353 atomicity is not accepted for #3106")
    if (
        manifest.payload.get("complete") is not True
        or manifest.payload.get("stage") != "test_manifest"
        or manifest.payload.get("state") != "terminal"
        or manifest.payload.get("outcome") != "quality_rejected"
        or manifest.payload.get("details", {}).get("reason") != "new_api_compile_only"
    ):
        raise RuntimeError("#7353 expected manifest quality rejection is unavailable")


def _patch_sections(raw_patch: str) -> dict[str, str]:
    matches = list(re.finditer(r"^diff --git a/(\S+) b/(\S+)\s*$", raw_patch, re.M))
    return {
        match.group(2): raw_patch[match.start() : matches[i + 1].start() if i + 1 < len(matches) else None]
        for i, match in enumerate(matches)
    }


def _safe_relative(value: Any) -> Path:
    if not isinstance(value, str):
        raise RuntimeError("#7353 evidence path is invalid")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise RuntimeError("#7353 evidence path escapes artifact root")
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


__all__ = ["write_luanti_7353_gate57_blocker"]
