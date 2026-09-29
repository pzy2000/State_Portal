"""Current-chain-bound hard admission terminal for Luanti #14123.

The sole authoritative issue is a feature request for RNG state persistence,
not a bug or regression.  Independently, the preserved fixed C++ unit test
calls the newly-added state accessors, so a production-only inverse cannot
compile it against the buggy API.  Both findings are static and terminal:
there must be no capsule, build, attempt, paid action, or formal trial.
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


_CANDIDATE = {"repository_key": "luanti", "change_number": 14123}
_ENUMERATION = ArtifactReference(
    "repository_enumeration/luanti/current/v2", "repository_enumeration", "2.0"
)
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/14123/source/v2", "candidate_source_packet", "2.0"
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/14123/screening/v2", "candidate_test_patch", "2.0"
)
_CHECKPOINT = ArtifactReference(
    "screening_checkpoint/luanti/14123/current/v2", "screening_checkpoint", "2.0"
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/14123/preannotation/v2", "source_atomicity", "2.0"
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/14123/test_manifest/v2", "candidate_stage", "2.0"
)
_TERMINAL = ArtifactReference(
    "candidate_gate14_terminal/luanti/14123/preannotation/v1",
    "candidate_gate14_terminal",
    "1.0",
)
_PRODUCTION_PATHS = (
    "builtin/game/features.lua",
    "games/devtest/mods/unittests/misc.lua",
    "src/noise.cpp",
    "src/noise.h",
    "src/script/lua_api/l_noise.cpp",
    "src/script/lua_api/l_noise.h",
)
_TEST_PATH = "src/unittest/test_random.cpp"


def write_luanti_14123_gate14_terminal(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Audit only current typed input and persist the zero-execution terminal."""

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "candidate-screening"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError(
            "#14123 terminal requires active candidate-screening authorization"
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
    manifest = _read_json(
        root / _relative(source.payload["raw_evidence_path"], "source manifest")
    )
    raw_patch = _read_text(
        root / _relative(patch.payload["raw_patch_path"], "raw patch")
    )
    source_audit = _audit_source(manifest)
    api_audit = _audit_api(raw_patch)
    if not source_audit["feature_not_regression"] or not api_audit["incompatible"]:
        raise RuntimeError("#14123 expected hard admission evidence is absent")

    payload: dict[str, Any] = {
        "candidate": dict(_CANDIDATE),
        "state": "terminal",
        "outcome": "quality_rejected_gate2_and_gate4",
        "terminal": True,
        "resumable": False,
        "reason": "authoritative_feature_request_and_preserved_fixed_test_calls_pr_introduced_rng_state_api",
        "classification": "feature_request_not_bug_or_regression",
        "primary_issue_number": 14107,
        "authoritative_issue_count": 1,
        "source_audit": source_audit,
        "gate4_mechanical_audit": api_audit,
        "gate_status": {
            "gate_1_source_authority": "passed",
            "gate_2_real_bug_or_regression_mapping": "failed_feature_request",
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
        raise RuntimeError("#14123 typed terminal reload failed")
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
        raise RuntimeError("#14123 enumeration is not Luanti")
    if (
        source.inputs != (_ENUMERATION,)
        or source.payload.get("candidate") != _CANDIDATE
        or source.payload.get("complete") is not True
    ):
        raise RuntimeError("#14123 source packet binding is invalid")
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
        raise RuntimeError("#14123 patch binding is invalid")
    if (
        checkpoint.inputs != (_ENUMERATION, _PATCH)
        or checkpoint.payload.get("candidate") != _CANDIDATE
        or checkpoint.payload.get("state") != "complete"
        or checkpoint.payload.get("outcome") != "patch_eligible"
        or checkpoint.payload.get("stage_artifact_id") != _PATCH.artifact_id
    ):
        raise RuntimeError("#14123 screening checkpoint is invalid")
    if (
        atomicity.inputs != (_ENUMERATION, _SOURCE, _PATCH, _CHECKPOINT)
        or atomicity.payload.get("candidate") != _CANDIDATE
        or atomicity.payload.get("complete") is not True
        or atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("authoritative_issue_numbers") != [14107]
        or atomicity.payload.get("primary_issue_number") != 14107
    ):
        raise RuntimeError("#14123 source atomicity binding is invalid")
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
        raise RuntimeError("#14123 test manifest binding is invalid")


def _audit_source(source_manifest: dict[str, Any]) -> dict[str, object]:
    issue = _find_issue(source_manifest, 14107)
    pull = source_manifest.get("pull")
    if not isinstance(pull, dict):
        raise RuntimeError("#14123 pull is invalid")
    issue_labels = _labels(issue)
    pull_labels = _labels(pull)
    issue_body = issue.get("body")
    pull_body = pull.get("body")
    if not isinstance(issue_body, str) or not isinstance(pull_body, str):
        raise RuntimeError("#14123 source body is invalid")
    issue_markers = (
        "there is no good way to persist the state of a PRNG",
        "### Solutions",
        "add a `state()` method",
    )
    if (
        "Feature request" not in issue_labels
        or "Feature ✨" not in pull_labels
        or not all(marker in issue_body for marker in issue_markers)
        or "Allow PsedoRandom and PcgRandom" not in pull_body
    ):
        raise RuntimeError("#14123 feature-request source markers drifted")
    return {
        "issue_title": issue.get("title"),
        "issue_labels": sorted(issue_labels),
        "pull_title": pull.get("title"),
        "pull_labels": sorted(pull_labels),
        "issue_markers": list(issue_markers),
        "feature_not_regression": True,
        "issue_reports_preexisting_bug_or_regression": False,
    }


def _audit_api(raw_patch: str) -> dict[str, object]:
    sections = _parse_patch(raw_patch)
    expected = set(
        _PRODUCTION_PATHS + (_TEST_PATH, "doc/lua_api.md", "games/devtest/.luacheckrc")
    )
    if set(sections) != expected:
        raise RuntimeError("#14123 raw patch path inventory drifted")
    production = tuple(line for path in _PRODUCTION_PATHS for line in sections[path])
    test = sections[_TEST_PATH]
    introduced = {
        "PseudoRandom::getState": any(
            line.startswith("+") and "getState() const" in line for line in production
        ),
        "PcgRandom::getState": any(
            line.startswith("+") and "getState(u64 state[2]) const" in line
            for line in production
        ),
        "PcgRandom::setState": any(
            line.startswith("+") and "setState(const u64 state[2])" in line
            for line in production
        ),
    }
    fixed_test_calls = [
        line[1:].strip()
        for line in test
        if line.startswith("+") and (".getState(" in line or ".setState(" in line)
    ]
    incompatible = (
        all(introduced.values())
        and any(".getState(" in line for line in fixed_test_calls)
        and any(".setState(" in line for line in fixed_test_calls)
    )
    return {
        "production_paths": list(_PRODUCTION_PATHS),
        "test_paths": [_TEST_PATH],
        "raw_patch_paths": sorted(sections),
        "inverse_scope_strict": True,
        "pr_introduced_api": introduced,
        "fixed_test_calls": fixed_test_calls,
        "fixed_only_api": incompatible,
        "buggy_compile_expected": False,
        "explanation": (
            "A production-only inverse removes getState/setState from noise.h/noise.cpp, "
            "while preserving the fixed test hunk that calls those members."
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
                raise RuntimeError("#14123 raw patch header is invalid")
            left, right = parts[2], parts[3]
            if (
                not left.startswith("a/")
                or not right.startswith("b/")
                or left[2:] != right[2:]
            ):
                raise RuntimeError("#14123 raw patch path is invalid")
            current = _relative(left[2:], "patch path")
            if current in sections:
                raise RuntimeError("#14123 raw patch has duplicate path")
            sections[current] = []
        if current is not None:
            sections[current].append(line)
    if not sections:
        raise RuntimeError("#14123 raw patch is empty")
    return {path: tuple(lines) for path, lines in sections.items()}


def _find_issue(source_manifest: dict[str, Any], number: int) -> dict[str, Any]:
    for item in source_manifest.get("issues", []):
        if (
            isinstance(item, dict)
            and item.get("number") == number
            and isinstance(item.get("issue"), dict)
        ):
            return item["issue"]
    raise RuntimeError("#14123 primary issue is missing")


def _labels(value: dict[str, Any]) -> set[str]:
    labels = value.get("labels")
    if not isinstance(labels, list):
        raise RuntimeError("#14123 labels are invalid")
    return {
        item["name"]
        for item in labels
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }


def _relative(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise RuntimeError(f"#14123 {label} path is invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {".", ".."} for part in path.parts)
        or path.as_posix() != value
    ):
        raise RuntimeError(f"#14123 {label} path is unsafe")
    return path.as_posix()


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("#14123 source manifest is unsafe")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("#14123 source manifest is invalid")
    return value


def _read_text(path: Path) -> str:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("#14123 raw patch is unsafe")
    return path.read_text(encoding="utf-8", errors="surrogateescape")


def _assert_absent(root: Path) -> None:
    path = root / "artifacts" / Path(_TERMINAL.artifact_id) / _TERMINAL.semantic_version
    if path.exists():
        raise RuntimeError("#14123 typed terminal already exists")


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


__all__ = ["write_luanti_14123_gate14_terminal"]
