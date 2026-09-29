"""Current-chain-bound hard admission terminal for Luanti #11495.

Its lone authoritative issue is a release-planning discussion, not a reported
defect or regression.  Separately, the new fixed unit test calls the new
``fmtgettext`` helper from the same PR, making it incompatible with a strict
production-only inverse.  The terminal is static and deliberately performs
no materialization, build, attempt, paid action, or formal trial.
"""

from __future__ import annotations

import json
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


_CANDIDATE = {"repository_key": "luanti", "change_number": 11495}
_ENUMERATION = ArtifactReference(
    "repository_enumeration/luanti/current/v2", "repository_enumeration", "2.0"
)
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/11495/source/v2", "candidate_source_packet", "2.0"
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/11495/screening/v2", "candidate_test_patch", "2.0"
)
_CHECKPOINT = ArtifactReference(
    "screening_checkpoint/luanti/11495/current/v2", "screening_checkpoint", "2.0"
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/11495/preannotation/v2", "source_atomicity", "2.0"
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/11495/test_manifest/v2", "candidate_stage", "2.0"
)
_TERMINAL = ArtifactReference(
    "candidate_gate14_terminal/luanti/11495/preannotation/v1",
    "candidate_gate14_terminal",
    "1.0",
)
_PRODUCTION_PATHS = (
    "builtin/fstk/ui.lua",
    "src/client/camera.cpp",
    "src/client/camera.h",
    "src/client/game.cpp",
    "src/gettext.h",
)
_TEST_PATH = "src/unittest/test_gettext.cpp"


def write_luanti_11495_gate14_terminal(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Persist and strictly reload the zero-execution terminal evidence."""

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "candidate-screening"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError(
            "#11495 terminal requires active candidate-screening authorization"
        )
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
    _validate_chain(*chain)
    source = chain[1]
    patch = chain[2]
    source_manifest = _read_json(
        root / _relative(source.payload["raw_evidence_path"], "source manifest")
    )
    raw_patch = _read_text(
        root / _relative(patch.payload["raw_patch_path"], "raw patch")
    )
    source_audit = _audit_source(source_manifest)
    api_audit = _audit_api(raw_patch)
    if (
        not source_audit["release_planning_not_regression"]
        or not api_audit["incompatible"]
    ):
        raise RuntimeError("#11495 expected hard-admission evidence is absent")

    payload: dict[str, Any] = {
        "candidate": dict(_CANDIDATE),
        "state": "terminal",
        "outcome": "quality_rejected_gate2_and_gate4",
        "terminal": True,
        "resumable": False,
        "reason": "authoritative_release_planning_discussion_and_preserved_fixed_test_calls_pr_introduced_fmtgettext",
        "classification": "release_planning_discussion_not_bug_or_regression",
        "primary_issue_number": 11600,
        "authoritative_issue_count": 1,
        "source_audit": source_audit,
        "gate4_mechanical_audit": api_audit,
        "gate_status": {
            "gate_1_source_authority": "passed",
            "gate_2_real_bug_or_regression_mapping": "failed_release_planning_discussion",
            "gate_3_production_only_inverse": "passed_mechanical",
            "gate_4_fixed_test_buggy_api_compatibility": "failed_fixed_only_api",
            "gate_5_fixed_f2p_3x": "not_run_hard_terminal",
            "gate_6_buggy_f2p_3x_failure": "not_run_hard_terminal",
            "gate_7_fixed_buggy_p2p_3x": "not_run_hard_terminal",
        },
        "current_v2_chain_revisions": {
            artifact.reference.artifact_id: artifact.created_at for artifact in chain
        },
        "candidate_execution_performed": False,
        "candidate_materialization_performed": False,
        "new_build_started": False,
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "fresh_epoch": {
            "required_attempt_count": 12,
            "attempt_count": 0,
            "not_run": True,
            "old_attempts_referenced": False,
        },
        "next_action": "do_not_schedule_capsule_build_or_gate57_execution; remove_candidate_from_gate57_pool",
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    inputs = tuple(artifact.reference for artifact in chain)
    _assert_absent(root)
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
        raise RuntimeError("#11495 typed terminal reload failed")
    return artifact


def _validate_chain(
    enumeration: StableArtifact,
    source: StableArtifact,
    patch: StableArtifact,
    checkpoint: StableArtifact,
    atomicity: StableArtifact,
    manifest: StableArtifact,
) -> None:
    if enumeration.payload.get("repository_key") != "luanti":
        raise RuntimeError("#11495 enumeration is not Luanti")
    if (
        source.inputs != (_ENUMERATION,)
        or source.payload.get("candidate") != _CANDIDATE
        or source.payload.get("complete") is not True
    ):
        raise RuntimeError("#11495 source packet binding is invalid")
    if (
        patch.inputs != (_ENUMERATION, _SOURCE)
        or patch.payload.get("candidate") != _CANDIDATE
        or patch.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
        or tuple(patch.payload.get("production_paths", ())) != _PRODUCTION_PATHS
        or tuple(patch.payload.get("test_paths", ())) != (_TEST_PATH,)
        or patch.payload.get("source_manifest_path")
        != source.payload.get("raw_evidence_path")
    ):
        raise RuntimeError("#11495 patch binding is invalid")
    if (
        checkpoint.inputs != (_ENUMERATION, _PATCH)
        or checkpoint.payload.get("candidate") != _CANDIDATE
        or checkpoint.payload.get("state") != "complete"
        or checkpoint.payload.get("outcome") != "patch_eligible"
        or checkpoint.payload.get("stage_artifact_id") != _PATCH.artifact_id
    ):
        raise RuntimeError("#11495 screening checkpoint is invalid")
    if (
        atomicity.inputs != (_ENUMERATION, _SOURCE, _PATCH, _CHECKPOINT)
        or atomicity.payload.get("candidate") != _CANDIDATE
        or atomicity.payload.get("complete") is not True
        or atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("authoritative_issue_numbers") != [11600]
        or atomicity.payload.get("primary_issue_number") != 11600
    ):
        raise RuntimeError("#11495 source atomicity binding is invalid")
    details = manifest.payload.get("details")
    if (
        manifest.inputs != (_PATCH,)
        or manifest.payload.get("candidate") != _CANDIDATE
        or manifest.payload.get("complete") is not True
        or manifest.payload.get("stage") != "test_manifest"
        or manifest.payload.get("state") != "passed"
        or not isinstance(details, dict)
        or details.get("executable_test_paths") != [_TEST_PATH]
        or details.get("test_count") != 1
    ):
        raise RuntimeError("#11495 test manifest binding is invalid")


def _audit_source(source_manifest: dict[str, Any]) -> dict[str, object]:
    issue = _find_issue(source_manifest, 11600)
    pull = source_manifest.get("pull")
    if not isinstance(pull, dict):
        raise RuntimeError("#11495 pull is invalid")
    labels = _labels(issue)
    issue_body = issue.get("body")
    if (
        "Discussion" not in labels
        or issue.get("title") != "5.5 release plans"
        or not isinstance(issue_body, str)
        or "## Background" not in issue_body
        or "## Proposal" not in issue_body
        or "release schedule" not in issue_body
    ):
        raise RuntimeError("#11495 release-plan issue markers drifted")
    return {
        "issue_title": issue.get("title"),
        "issue_labels": sorted(labels),
        "pull_title": pull.get("title"),
        "pull_labels": sorted(_labels(pull)),
        "issue_is_release_planning_discussion": True,
        "issue_reports_preexisting_bug_or_regression": False,
        "release_planning_not_regression": True,
    }


def _audit_api(raw_patch: str) -> dict[str, object]:
    sections = _parse_patch(raw_patch)
    expected_paths = set(
        _PRODUCTION_PATHS
        + (_TEST_PATH, "src/unittest/CMakeLists.txt", "util/updatepo.sh")
    )
    if set(sections) != expected_paths:
        raise RuntimeError("#11495 raw patch path inventory drifted")
    gettext_hunk = sections["src/gettext.h"]
    test_hunk = sections[_TEST_PATH]
    helper_added = any(
        line.startswith("+") and "inline std::string fmtgettext(" in line
        for line in gettext_hunk
    )
    test_calls = [
        line[1:].strip()
        for line in test_hunk
        if line.startswith("+") and "fmtgettext(" in line
    ]
    incompatible = helper_added and bool(test_calls)
    return {
        "production_paths": list(_PRODUCTION_PATHS),
        "test_paths": [_TEST_PATH],
        "raw_patch_paths": sorted(sections),
        "inverse_scope_strict": True,
        "pr_introduced_api": {"fmtgettext": helper_added},
        "fixed_test_calls": test_calls,
        "fixed_only_api": incompatible,
        "buggy_compile_expected": False,
        "explanation": (
            "The production-only inverse removes fmtgettext from gettext.h but preserves "
            "the fixed TestGettext::testFmtgettext calls."
        ),
        "incompatible": incompatible,
    }


def _parse_patch(raw_patch: str) -> dict[str, tuple[str, ...]]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in raw_patch.splitlines(keepends=True):
        if line.startswith("diff --git "):
            parts = shlex.split(line.rstrip("\r\n"))
            if len(parts) != 4 or parts[:2] != ["diff", "--git"]:
                raise RuntimeError("#11495 raw patch header is invalid")
            left, right = parts[2], parts[3]
            if (
                not left.startswith("a/")
                or not right.startswith("b/")
                or left[2:] != right[2:]
            ):
                raise RuntimeError("#11495 raw patch path is invalid")
            current = _relative(left[2:], "patch path")
            if current in sections:
                raise RuntimeError("#11495 raw patch has duplicate path")
            sections[current] = []
        if current is not None:
            sections[current].append(line)
    if not sections:
        raise RuntimeError("#11495 raw patch is empty")
    return {path: tuple(lines) for path, lines in sections.items()}


def _find_issue(source_manifest: dict[str, Any], number: int) -> dict[str, Any]:
    for item in source_manifest.get("issues", []):
        if (
            isinstance(item, dict)
            and item.get("number") == number
            and isinstance(item.get("issue"), dict)
        ):
            return item["issue"]
    raise RuntimeError("#11495 primary issue is missing")


def _labels(value: dict[str, Any]) -> set[str]:
    labels = value.get("labels")
    if not isinstance(labels, list):
        raise RuntimeError("#11495 labels are invalid")
    return {
        item["name"]
        for item in labels
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }


def _relative(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise RuntimeError(f"#11495 {label} path is invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {".", ".."} for part in path.parts)
        or path.as_posix() != value
    ):
        raise RuntimeError(f"#11495 {label} path is unsafe")
    return path.as_posix()


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("#11495 source manifest is unsafe")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("#11495 source manifest is invalid")
    return value


def _read_text(path: Path) -> str:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("#11495 raw patch is unsafe")
    return path.read_text(encoding="utf-8", errors="surrogateescape")


def _assert_absent(root: Path) -> None:
    path = root / "artifacts" / Path(_TERMINAL.artifact_id) / _TERMINAL.semantic_version
    if path.exists():
        raise RuntimeError("#11495 typed terminal already exists")


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["write_luanti_11495_gate14_terminal"]
