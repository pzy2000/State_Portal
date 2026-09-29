"""Candidate-specific hard Gate 2 terminal for Luanti #12261.

The current-v2 chain is mechanically sound through Gate 4: the inverse only
changes production files and the preserved serialization tests call the same
public APIs in both revisions.  Its sole authoritative issue, however, is
explicitly a ``Performance`` / ``Request / Suggestion`` discussion about
making a hot path faster.  It is not a reported defect or regression, so the
candidate cannot enter the single-defect benchmark and must stop before any
capsule, build, attempt, paid action, or formal trial.
"""

from __future__ import annotations

import json
import os
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


_CANDIDATE = {"repository_key": "luanti", "change_number": 12261}
_ENUMERATION = ArtifactReference(
    "repository_enumeration/luanti/current/v2", "repository_enumeration", "2.0"
)
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/12261/source/v2", "candidate_source_packet", "2.0"
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/12261/screening/v2", "candidate_test_patch", "2.0"
)
_CHECKPOINT = ArtifactReference(
    "screening_checkpoint/luanti/12261/current/v2", "screening_checkpoint", "2.0"
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/12261/preannotation/v2", "source_atomicity", "2.0"
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/12261/test_manifest/v2", "candidate_stage", "2.0"
)
_TERMINAL = ArtifactReference(
    "candidate_gate14_terminal/luanti/12261/preannotation/v1",
    "candidate_gate14_terminal",
    "1.0",
)

_PRODUCTION_PATHS = ("src/unittest/test.h", "src/util/serialize.cpp")
_TEST_PATHS = ("src/unittest/test_serialization.cpp",)
_REQUIRED_TEST_APIS = (
    "serializeJsonString(",
    "deSerializeJsonString(",
    "serializeJsonStringIfNeeded(",
    "deSerializeJsonStringIfNeeded(",
    "UASSERTEQ(",
)


def write_luanti_12261_gate14_terminal(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Persist the current-chain-bound quality terminal and reload it strictly."""

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "candidate-screening"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#12261 terminal requires active candidate-screening authorization")
    root = Path(artifact_root).absolute().resolve(strict=True)
    enumeration, source, patch, checkpoint, atomicity, manifest = tuple(
        _load(root, reference)
        for reference in (_ENUMERATION, _SOURCE, _PATCH, _CHECKPOINT, _ATOMICITY, _MANIFEST)
    )
    _validate_typed_chain(enumeration, source, patch, checkpoint, atomicity, manifest)

    source_path = _relative(source.payload.get("raw_evidence_path"), "source packet")
    patch_path = _relative(patch.payload.get("raw_patch_path"), "patch receipt")
    source_manifest = _read_json(root / source_path, "source manifest")
    raw_patch = _read_text(root / patch_path, "raw patch")
    issue = _find_issue(source_manifest, 12250)
    issue_labels = _labels(issue)
    issue_body = issue.get("body")
    if not isinstance(issue_body, str):
        raise RuntimeError("#12261 authoritative issue body is invalid")
    pull = source_manifest.get("pull")
    if not isinstance(pull, dict):
        raise RuntimeError("#12261 source manifest pull is invalid")
    pull_labels = _labels(pull)
    pull_body = pull.get("body")
    if not isinstance(pull_body, str):
        raise RuntimeError("#12261 pull body is invalid")

    # This is a narrow Gate 4 mechanical audit, not a substitute test run.
    # The patch preserves every test-facing serialization function signature;
    # the only test.h change is a diagnostic-label whitespace change.
    _validate_patch_surface(raw_patch)
    if not {"Performance", "Request / Suggestion"}.issubset(issue_labels):
        raise RuntimeError("#12261 non-regression issue labels are incomplete")
    issue_markers = (
        "shows up frequently in profiling",
        "std::stringstream has pathologically bad performance",
        "## Solutions",
        "placeholder to keep track of the discussion",
    )
    if not all(marker in issue_body for marker in issue_markers):
        raise RuntimeError("#12261 performance-request issue markers are incomplete")
    if "benchmark results" not in pull_body or "regressions were fixed" not in pull_body:
        raise RuntimeError("#12261 pull semantics do not bind to the performance request")

    _assert_absent(root)
    payload: dict[str, Any] = {
        "candidate": dict(_CANDIDATE),
        "state": "terminal",
        "outcome": "quality_rejected",
        "terminal": True,
        "resumable": False,
        "reason": "authoritative_performance_improvement_request_not_bug_or_regression",
        "classification": "performance_optimization_request",
        "primary_issue_number": 12250,
        "authoritative_issue_count": 1,
        "pull_title": pull.get("title"),
        "pull_labels": sorted(pull_labels),
        "issue_labels": sorted(issue_labels),
        "source_semantics": {
            "issue_is_performance_request": True,
            "issue_is_request_suggestion": True,
            "issue_reports_preexisting_bug_or_regression": False,
            "issue_markers": list(issue_markers),
            "pull_body_marks_regressions_fixed_during_optimization": True,
        },
        "gate4_mechanical_audit": {
            "production_paths": list(_PRODUCTION_PATHS),
            "test_paths": list(_TEST_PATHS),
            "test_h_change": "diagnostic_label_spacing_only",
            "preserved_test_api_calls": list(_REQUIRED_TEST_APIS),
            "fixed_only_test_api_observed": False,
            "buggy_api_surface_compatible": True,
            "candidate_execution_performed": False,
        },
        "gate_status": {
            "gate_1_source_authority": "passed",
            "gate_2_real_bug_or_regression_mapping": "failed_performance_request",
            "gate_3_production_only_inverse": "mechanically_eligible_but_not_admitted",
            "gate_4_fixed_test_buggy_api_compatibility": "passed_mechanical",
            "gate_5_fixed_f2p_3x": "not_run_gate2_terminal",
            "gate_6_buggy_f2p_3x_failure": "not_run_gate2_terminal",
            "gate_7_fixed_buggy_p2p_3x": "not_run_gate2_terminal",
        },
        "candidate_materialization_performed": False,
        "new_build_started": False,
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    inputs = (_ENUMERATION, _SOURCE, _PATCH, _CHECKPOINT, _ATOMICITY, _MANIFEST)
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
        raise RuntimeError("#12261 terminal typed reload failed")
    return artifact


def _validate_typed_chain(
    enumeration: StableArtifact,
    source: StableArtifact,
    patch: StableArtifact,
    checkpoint: StableArtifact,
    atomicity: StableArtifact,
    manifest: StableArtifact,
) -> None:
    if enumeration.payload.get("repository_key") != "luanti":
        raise RuntimeError("#12261 enumeration is not Luanti")
    if (
        source.inputs != (_ENUMERATION,)
        or source.payload.get("candidate") != _CANDIDATE
        or source.payload.get("complete") is not True
    ):
        raise RuntimeError("#12261 source packet binding is invalid")
    if (
        patch.inputs != (_ENUMERATION, _SOURCE)
        or patch.payload.get("candidate") != _CANDIDATE
        or patch.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
        or tuple(patch.payload.get("production_paths", ())) != _PRODUCTION_PATHS
        or tuple(patch.payload.get("test_paths", ())) != _TEST_PATHS
        or patch.payload.get("source_manifest_path") != source.payload.get("raw_evidence_path")
    ):
        raise RuntimeError("#12261 patch binding is invalid")
    if (
        checkpoint.payload.get("candidate") != _CANDIDATE
        or checkpoint.payload.get("state") != "complete"
        or checkpoint.payload.get("stage_artifact_id") != _PATCH.artifact_id
    ):
        raise RuntimeError("#12261 screening checkpoint is invalid")
    if (
        atomicity.inputs != (_ENUMERATION, _SOURCE, _PATCH, _CHECKPOINT)
        or atomicity.payload.get("candidate") != _CANDIDATE
        or atomicity.payload.get("complete") is not True
        or atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("authoritative_issue_numbers") != [12250]
        or atomicity.payload.get("primary_issue_number") != 12250
    ):
        raise RuntimeError("#12261 atomicity receipt is invalid")
    details = manifest.payload.get("details")
    if (
        manifest.inputs != (_PATCH,)
        or manifest.payload.get("candidate") != _CANDIDATE
        or manifest.payload.get("complete") is not True
        or manifest.payload.get("stage") != "test_manifest"
        or manifest.payload.get("state") != "passed"
        or not isinstance(details, dict)
        or details.get("executable_test_paths") != list(_TEST_PATHS)
        or details.get("test_count") != 1
    ):
        raise RuntimeError("#12261 test manifest is invalid")


def _validate_patch_surface(raw_patch: str) -> None:
    expected_paths = {
        "src/unittest/test.h",
        "src/unittest/test_serialization.cpp",
        "src/util/serialize.cpp",
    }
    paths = {line.removeprefix("diff --git a/").split(" b/", 1)[0] for line in raw_patch.splitlines() if line.startswith("diff --git a/")}
    if paths != expected_paths:
        raise RuntimeError("#12261 raw patch path inventory drifted")
    if '<< "    actual:   " << a' not in raw_patch or '<< "    actual  : " << a' not in raw_patch:
        raise RuntimeError("#12261 expected test.h diagnostic-only hunk is missing")
    for api in _REQUIRED_TEST_APIS:
        if api not in raw_patch:
            raise RuntimeError(f"#12261 preserved test API marker is missing: {api}")
    for signature in (
        "std::string serializeJsonString(const std::string &plain)",
        "std::string deSerializeJsonString(std::istream &is)",
        "std::string deSerializeJsonStringIfNeeded(std::istream &is)",
    ):
        if signature not in raw_patch:
            raise RuntimeError(f"#12261 unchanged API signature marker is missing: {signature}")


def _find_issue(source_manifest: dict[str, Any], number: int) -> dict[str, Any]:
    for item in source_manifest.get("issues", []):
        if isinstance(item, dict) and item.get("number") == number and isinstance(item.get("issue"), dict):
            return item["issue"]
    raise RuntimeError("#12261 primary issue is missing from source manifest")


def _labels(value: dict[str, Any]) -> set[str]:
    labels = value.get("labels")
    if not isinstance(labels, list):
        raise RuntimeError("#12261 labels are invalid")
    return {item["name"] for item in labels if isinstance(item, dict) and isinstance(item.get("name"), str)}


def _relative(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise RuntimeError(f"#12261 {label} path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {".", ".."} for part in path.parts) or path.as_posix() != value:
        raise RuntimeError(f"#12261 {label} path is unsafe")
    return path.as_posix()


def _read_json(path: Path, label: str) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"#12261 {label} is invalid")
    return value


def _read_text(path: Path, label: str) -> str:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"#12261 {label} is not a safe regular file")
    return path.read_text(encoding="utf-8", errors="surrogateescape")


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _assert_absent(root: Path) -> None:
    path = root / "artifacts" / Path(_TERMINAL.artifact_id) / _TERMINAL.semantic_version
    if path.exists():
        raise RuntimeError("#12261 typed terminal already exists")


def _now() -> str:
    return (datetime.now(UTC) + timedelta(microseconds=1)).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["write_luanti_12261_gate14_terminal"]
