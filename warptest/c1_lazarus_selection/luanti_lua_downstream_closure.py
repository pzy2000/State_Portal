"""Prepare admitted Luanti Lua candidates for bounded runtime/visual execution.

This module registers the deterministic Start, ordered replay, semantic oracle
and golden contract for the two high-priority Lua candidates.  Preparation is
not execution: a missing application pair or launch receipt remains downstream
work and never demotes the already-qualified source/patch/test admission.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Mapping

from .luanti_lua_downstream import (
    LuantiLuaTaskMapping,
    describe_luanti_lua_task_mapping,
)
from .luanti_lua_regression import profile_for_candidate
from .luanti_lua_visual_closure import (
    REQUIRED_RUNTIME_VISUAL_EVIDENCE,
    evaluate_luanti_lua_version_materialization,
)
from .models import CandidateKey
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_V2 = "2.0"
_V3 = "3.0"
_V4 = "4.0"
# All four admitted Lua profiles now have a registered downstream mapping.
# Keep this candidate-specific adapter bounded to that closed set; adding a
# number here requires a matching profile, mapping, and typed unit boundary.
_SUPPORTED = frozenset({9722, 12703, 13132, 14757})
_GOLDEN_CONTRACT = {
    "common_before_fixed_after_buggy_after": True,
    "pairwise_before_visual_equivalence": True,
    "two_by_two_after_cross_matrix": True,
    "raw_frames_required": True,
    "semantic_observations_required": True,
    "same_lineage_required": True,
}


@dataclass(frozen=True)
class LuantiLuaDownstreamPreparation:
    candidate: CandidateKey
    mapping: StableArtifact
    preflight: StableArtifact
    application_pair_status: str
    ready_for_bounded_launch: bool


@dataclass(frozen=True)
class _ApplicationPairAudit:
    status: str
    registered_paths: tuple[str, ...]
    missing_versions: tuple[str, ...]

    @property
    def ready_for_bounded_launch(self) -> bool:
        return self.status == "complete_launch_unverified"


def prepare_luanti_lua_downstream_closure(
    artifact_root: str | Path,
    *,
    candidate: CandidateKey,
    authorization: AuthorizationRecord,
) -> LuantiLuaDownstreamPreparation:
    """Register the execution design and persist a resumable pending receipt."""

    _require_authorization(authorization)
    root = _ordinary_root(Path(artifact_root).absolute())
    transition, stage, checkpoint = _load_active_boundary(root, candidate)
    mapping_definition = describe_luanti_lua_task_mapping(candidate)
    fixture_files = _fixture_files(mapping_definition)
    fixture_root = _write_fixture(root, mapping_definition, fixture_files)
    mapping = _write_or_resume_mapping(
        root,
        mapping_definition,
        transition=transition.reference,
        fixture_root=fixture_root,
        fixture_files=fixture_files,
        authorization=authorization,
    )
    pair = _audit_application_pair(root, candidate)
    payload = _preflight_payload(candidate, pair)
    reference = _preflight_reference(candidate)
    inputs = (
        transition.reference,
        stage.reference,
        checkpoint.reference,
        mapping.reference,
    )
    existing = _try_load(root, reference)
    if existing is None or existing.inputs != inputs or existing.payload != payload:
        write_stable_artifact(
            root,
            artifact_id=reference.artifact_id,
            artifact_kind=reference.artifact_kind,
            semantic_version=reference.semantic_version,
            created_at=_now(),
            inputs=inputs,
            payload=payload,
            authorization=authorization,
        )
    preflight = load_luanti_lua_downstream_preflight(root, candidate=candidate)
    return LuantiLuaDownstreamPreparation(
        candidate=candidate,
        mapping=mapping,
        preflight=preflight,
        application_pair_status=pair.status,
        ready_for_bounded_launch=pair.ready_for_bounded_launch,
    )


def load_luanti_lua_downstream_preflight(
    artifact_root: str | Path,
    *,
    candidate: CandidateKey,
) -> StableArtifact:
    """Strictly reload the current pending receipt and all registered assets."""

    root = _ordinary_root(Path(artifact_root).absolute())
    transition, stage, checkpoint = _load_active_boundary(root, candidate)
    definition = describe_luanti_lua_task_mapping(candidate)
    fixture_files = _fixture_files(definition)
    fixture_root = _fixture_root(definition)
    mapping = _load(root, _mapping_reference(definition))
    expected_mapping = _mapping_payload(definition, fixture_root, fixture_files)
    if mapping.inputs != (transition.reference,) or mapping.payload != expected_mapping:
        raise RuntimeError("Luanti Lua task mapping artifact is invalid")
    _validate_fixture(root, fixture_root, fixture_files)
    pair = _audit_application_pair(root, candidate)
    artifact = _load(root, _preflight_reference(candidate))
    expected_inputs = (
        transition.reference,
        stage.reference,
        checkpoint.reference,
        mapping.reference,
    )
    if artifact.inputs != expected_inputs:
        raise RuntimeError("Luanti Lua downstream preflight binding is invalid")
    if artifact.payload != _preflight_payload(candidate, pair):
        raise RuntimeError("Luanti Lua downstream asset inventory changed")
    return artifact


def _load_active_boundary(
    root: Path,
    candidate: CandidateKey,
) -> tuple[StableArtifact, StableArtifact, StableArtifact]:
    if (
        type(candidate) is not CandidateKey
        or candidate.repository_key != "luanti"
        or candidate.change_number not in _SUPPORTED
    ):
        raise ValueError("Luanti Lua downstream candidate is unsupported")
    decision = evaluate_luanti_lua_version_materialization(root, candidate=candidate)
    if not decision.unit_evidence_qualified:
        raise RuntimeError("Luanti Lua admission is not qualified")
    transition = _load(root, decision.unit_transition)
    stage_reference = ArtifactReference(
        f"candidate_stage/luanti/{candidate.change_number}/test_repetitions/v2",
        "candidate_stage",
        _V2,
    )
    stage = _load(root, stage_reference)
    checkpoint = _load(
        root,
        ArtifactReference(
            f"preannotation_checkpoint/luanti/{candidate.change_number}/current/v2",
            "preannotation_checkpoint",
            _V2,
        ),
    )
    expected_checkpoint = {
        "candidate": candidate.to_value(),
        "completed_stages": ["test_manifest", "test_repetitions"],
        "state": "active",
        "outcome": None,
        "current_artifact_id": stage.reference.artifact_id,
    }
    if (
        checkpoint.inputs != (stage.reference,)
        or checkpoint.payload != expected_checkpoint
    ):
        raise RuntimeError("Luanti Lua downstream checkpoint is not active")
    return transition, stage, checkpoint


def _fixture_files(mapping: LuantiLuaTaskMapping) -> dict[str, str]:
    semantic_oracle = {
        "goal": mapping.goal_check,
        "defect": mapping.defect_signature,
        "invariants": list(mapping.invariant_checks),
    }
    return {
        "game.conf": f"name = {mapping.task_id}\ntitle = {mapping.task_id}\n",
        "minetest.conf": (
            "name = warptest\n"
            "mg_name = flat\n"
            "creative_mode = true\n"
            "enable_damage = false\n"
        ),
        **dict(mapping.fixture_files),
        "start.json": _json_text(dict(mapping.start_definition)),
        "replay.json": _json_text(list(mapping.replay_actions)),
        "oracle.json": _json_text(semantic_oracle),
        "golden-contract.json": _json_text(_GOLDEN_CONTRACT),
    }


def _fixture_root(mapping: LuantiLuaTaskMapping) -> str:
    revision = "v2" if mapping.candidate.change_number == 14757 else "v1"
    return (
        f"private/downstream-fixtures/luanti/{mapping.candidate.change_number}/"
        f"{mapping.task_id}/{revision}"
    )


def _write_fixture(
    root: Path,
    mapping: LuantiLuaTaskMapping,
    files: Mapping[str, str],
) -> str:
    relative_root = _fixture_root(mapping)
    fixture = root / PurePosixPath(relative_root)
    _make_safe_directory(fixture, root)
    for relative, content in files.items():
        target = fixture / PurePosixPath(relative)
        _make_safe_directory(target.parent, fixture)
        if target.exists() or target.is_symlink():
            _ordinary_file(target, fixture)
            if target.read_text(encoding="utf-8") != content:
                raise RuntimeError("Luanti Lua downstream fixture content changed")
        else:
            target.write_text(content, encoding="utf-8")
            target.chmod(0o600)
    _validate_fixture(root, relative_root, files)
    return relative_root


def _validate_fixture(root: Path, relative_root: str, files: Mapping[str, str]) -> None:
    fixture = _ordinary_directory(root / PurePosixPath(relative_root), root)
    actual: list[str] = []
    for path in fixture.rglob("*"):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError("Luanti Lua downstream fixture is unsafe")
        if stat.S_ISREG(info.st_mode):
            actual.append(path.relative_to(fixture).as_posix())
    if sorted(actual) != sorted(files):
        raise RuntimeError("Luanti Lua downstream fixture inventory changed")
    for relative, expected in files.items():
        path = _ordinary_file(fixture / PurePosixPath(relative), fixture)
        if path.read_text(encoding="utf-8") != expected:
            raise RuntimeError("Luanti Lua downstream fixture content changed")


def _mapping_reference(mapping: LuantiLuaTaskMapping) -> ArtifactReference:
    semantic_version = _V4 if mapping.candidate.change_number == 14757 else _V3
    version = "v4" if semantic_version == _V4 else "v3"
    return ArtifactReference(
        f"lazarus_task_mapping/luanti/{mapping.candidate.change_number}/"
        f"{mapping.task_id}/{version}",
        "lazarus_task_mapping",
        semantic_version,
    )


def _mapping_payload(
    mapping: LuantiLuaTaskMapping,
    fixture_root: str,
    fixture_files: Mapping[str, str],
) -> dict[str, object]:
    return {
        "candidate": mapping.candidate.to_value(),
        "issue_number": mapping.issue_number,
        "task_id": mapping.task_id,
        "profile_id": mapping.profile_id,
        "defect_locus": mapping.defect_locus,
        "registered_start": dict(mapping.start_definition),
        "ordered_replay": [dict(action) for action in mapping.replay_actions],
        "semantic_oracle": {
            "goal": mapping.goal_check,
            "defect": mapping.defect_signature,
            "invariants": list(mapping.invariant_checks),
        },
        "golden_contract": dict(_GOLDEN_CONTRACT),
        "fixture_root": fixture_root,
        "fixture_files": sorted(fixture_files),
        "candidate_execution_performed": False,
        "complete": True,
    }


def _write_or_resume_mapping(
    root: Path,
    mapping: LuantiLuaTaskMapping,
    *,
    transition: ArtifactReference,
    fixture_root: str,
    fixture_files: Mapping[str, str],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    reference = _mapping_reference(mapping)
    payload = _mapping_payload(mapping, fixture_root, fixture_files)
    existing = _try_load(root, reference)
    if existing is not None:
        if existing.inputs != (transition,) or existing.payload != payload:
            raise RuntimeError("Luanti Lua task mapping artifact is invalid")
        return existing
    write_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
        created_at=_now(),
        inputs=(transition,),
        payload=payload,
        authorization=authorization,
    )
    return _load(root, reference)


def _audit_application_pair(
    root: Path, candidate: CandidateKey
) -> _ApplicationPairAudit:
    profile = profile_for_candidate(candidate)
    base_relative = f"private/luanti-{candidate.change_number}-formal-materialization"
    base = root / PurePosixPath(base_relative)
    registered: list[str] = []
    missing: list[str] = []
    for version in ("fixed", "buggy"):
        tree = base / version
        if not tree.exists() and not tree.is_symlink():
            missing.append(version)
            continue
        try:
            _ordinary_directory(tree, root)
        except RuntimeError as error:
            raise RuntimeError("application version root is unsafe") from error
        for relative in profile.production_paths:
            _ordinary_file(tree / PurePosixPath(relative), tree)
        binary = _ordinary_file(tree / "bin/minetest", tree)
        if not os.access(binary, os.X_OK):
            raise RuntimeError("application binary is not executable")
        contents = _ordinary_directory(
            tree / "build-app/out/minetest.app/Contents", tree
        )
        _ordinary_file(contents / "Info.plist", contents)
        app_binary = _ordinary_file(contents / "MacOS/minetest", contents)
        if not os.access(app_binary, os.X_OK):
            raise RuntimeError("application bundle binary is not executable")
        _ordinary_directory(contents / "Resources/builtin", contents)
        registered.append(tree.relative_to(root).as_posix())
    status = "missing" if missing else "complete_launch_unverified"
    return _ApplicationPairAudit(status, tuple(registered), tuple(missing))


def _preflight_reference(candidate: CandidateKey) -> ArtifactReference:
    semantic_version = _V4 if candidate.change_number == 14757 else _V3
    version = "v4" if semantic_version == _V4 else "v3"
    return ArtifactReference(
        f"downstream_preflight/luanti/{candidate.change_number}/"
        f"runtime-visual/{version}",
        "downstream_preflight",
        semantic_version,
    )


def _preflight_payload(
    candidate: CandidateKey,
    pair: _ApplicationPairAudit,
) -> dict[str, object]:
    missing = list(REQUIRED_RUNTIME_VISUAL_EVIDENCE)
    if pair.ready_for_bounded_launch:
        missing.remove("complete_fixed_buggy_application_version_pair")
    return {
        "candidate": candidate.to_value(),
        "admission_state": "active_qualified",
        "downstream_state": "pending",
        "outcome": None,
        "reason": (
            "bounded_application_launch_receipt_missing"
            if pair.ready_for_bounded_launch
            else "complete_application_version_pair_missing"
        ),
        "application_pair_status": pair.status,
        "registered_application_paths": list(pair.registered_paths),
        "missing_application_versions": list(pair.missing_versions),
        "missing_required_evidence": missing,
        "next_action": (
            "validate_bounded_application_launch"
            if pair.ready_for_bounded_launch
            else "materialize_complete_application_pair"
        ),
        "golden_disposition": "required_no_not_applicable",
        "candidate_execution_performed": False,
        "permanent_terminal_written": False,
        "paid_actions_authorized": False,
        "complete": True,
    }


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _try_load(root: Path, reference: ArtifactReference) -> StableArtifact | None:
    try:
        return _load(root, reference)
    except RuntimeError as error:
        if str(error) in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            return None
        raise


def _ordinary_root(path: Path) -> Path:
    try:
        info = path.lstat()
    except FileNotFoundError as error:
        raise RuntimeError("artifact root is missing") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("artifact root is unsafe")
    return path.resolve(strict=True)


def _ordinary_directory(path: Path, root: Path) -> Path:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (FileNotFoundError, ValueError) as error:
        raise RuntimeError("required directory is missing or unsafe") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("required directory is missing or unsafe")
    return resolved


def _ordinary_file(path: Path, root: Path) -> Path:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (FileNotFoundError, ValueError) as error:
        raise RuntimeError("required file is missing or unsafe") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError("required file is missing or unsafe")
    return resolved


def _make_safe_directory(path: Path, root: Path) -> None:
    root = root.resolve(strict=True)
    try:
        relative = path.absolute().relative_to(root)
    except ValueError as error:
        raise RuntimeError("fixture path escapes artifact root") from error
    current = root
    for part in relative.parts:
        current = current / part
        if current.exists() or current.is_symlink():
            _ordinary_directory(current, root)
        else:
            current.mkdir(mode=0o700)


def _json_text(value: object) -> str:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    )


def _require_authorization(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("preannotation execution authorization is required")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = [
    "LuantiLuaDownstreamPreparation",
    "load_luanti_lua_downstream_preflight",
    "prepare_luanti_lua_downstream_closure",
]
