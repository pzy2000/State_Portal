"""Fail-closed bridge from Luanti Lua unit evidence to runtime construction.

The two registered Lua profiles have strong fixed/buggy unit-test evidence,
but a unit-test capsule is not a runnable C1-Lazarus application version.  This
module mechanically reloads that completed evidence before reporting the exact
runtime/world/visual evidence still required by the formal construction
contract.  It performs no execution and writes no artifact.
"""

from __future__ import annotations

import base64
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .luanti_lua_regression import LuaRegressionProfile, profile_for_candidate
from .models import CandidateKey
from .stable_protocol import ArtifactReference, StableArtifact, load_stable_artifact


REQUIRED_RUNTIME_VISUAL_EVIDENCE = (
    "complete_fixed_buggy_application_version_pair",
    "ordered_reference_replay_with_actions_and_waits",
    "raw_fixed_and_buggy_start_observations",
    "semantic_goal_defect_and_invariant_observations",
    "common_before_fixed_after_and_buggy_after_goldens",
    "pairwise_before_visual_equivalence",
    "two_by_two_after_visual_cross_matrix",
    "same_lineage_construction_receipt",
)

_ATTEMPT_KEYS = frozenset(
    {
        "candidate",
        "profile_id",
        "phase",
        "variant",
        "repetition",
        "selector",
        "test_path",
        "expected_outcome",
        "observed_outcome",
        "process_outcome",
        "returncode",
        "completed_test_count",
        "stdout_base64",
        "stderr_base64",
        "stdout_truncated",
        "stderr_truncated",
        "output_encoding",
        "matched_expected",
    }
)
_AGGREGATE_KEYS = frozenset(
    {
        "candidate",
        "profile_id",
        "repetitions_per_cell",
        "attempt_count",
        "stable_repetitions",
        "all_expected",
        "qualified",
        "cells",
    }
)
_TRANSITION_KEYS = frozenset(
    {
        "candidate",
        "profile_id",
        "repetitions_per_cell",
        "attempt_count",
        "stable_repetitions",
        "all_expected",
        "qualified",
        "truth_table",
        "complete",
    }
)


@dataclass(frozen=True)
class LuaRuntimeVisualClosureBlocker:
    """Typed, read-only result that cannot be persisted as a pipeline stage."""

    candidate: CandidateKey
    unit_transition: ArtifactReference
    reason: str
    missing_required_evidence: tuple[str, ...]
    golden_disposition: str
    unit_evidence_qualified: bool = True

    @property
    def ready(self) -> bool:
        return False


def evaluate_luanti_lua_version_materialization(
    artifact_root: str | Path,
    *,
    candidate: CandidateKey,
) -> LuaRuntimeVisualClosureBlocker:
    """Reload qualified unit evidence, then refuse to call it a full version.

    A future registered runtime/world adapter may replace this terminal
    operation for a new candidate revision.  Until such evidence exists, the
    formal golden contract has no ``not_applicable`` escape hatch.
    """

    root = _ordinary_root(Path(artifact_root).absolute())
    profile = profile_for_candidate(candidate)
    manifest = _load_manifest(root, profile)
    capsule = _load_capsule(root, profile)
    attempts = tuple(_load_attempts(root, profile, capsule.reference))
    aggregate = _load_aggregate(root, profile, attempts)
    transition = _load_transition(
        root,
        profile,
        manifest=manifest.reference,
        capsule=capsule.reference,
        attempts=tuple(item.reference for item in attempts),
        aggregate=aggregate.reference,
    )
    _load_test_repetitions_stage(
        root,
        profile,
        manifest=manifest.reference,
        transition=transition.reference,
    )
    return LuaRuntimeVisualClosureBlocker(
        candidate=profile.candidate,
        unit_transition=transition.reference,
        reason="registered_runtime_world_visual_closure_missing",
        missing_required_evidence=REQUIRED_RUNTIME_VISUAL_EVIDENCE,
        golden_disposition="required_no_not_applicable",
    )


def _manifest_reference(profile: LuaRegressionProfile) -> ArtifactReference:
    return ArtifactReference(
        f"candidate_stage/luanti/{profile.candidate.change_number}/test_manifest/v2",
        "candidate_stage",
        "2.0",
    )


def _transition_reference(profile: LuaRegressionProfile) -> ArtifactReference:
    return ArtifactReference(
        (
            "luanti_lua_regression_transition/luanti/"
            f"{profile.candidate.change_number}/test-repetitions/v2"
        ),
        "luanti_lua_regression_transition",
        "2.0",
    )


def _aggregate_reference(profile: LuaRegressionProfile) -> ArtifactReference:
    return ArtifactReference(
        (
            "lua_regression_aggregate/luanti/"
            f"{profile.candidate.change_number}/fixed-buggy/v3"
        ),
        "lua_regression_aggregate",
        "3.0",
    )


def _attempt_reference(
    profile: LuaRegressionProfile,
    phase: str,
    variant: str,
    repetition: int,
) -> ArtifactReference:
    return ArtifactReference(
        (
            f"lua_regression_attempt/luanti/{profile.candidate.change_number}/"
            f"{phase}/{variant}/{repetition}/v3"
        ),
        "lua_regression_attempt",
        "3.0",
    )


def _load_manifest(root: Path, profile: LuaRegressionProfile) -> StableArtifact:
    reference = _manifest_reference(profile)
    artifact = _load(root, reference)
    payload = artifact.payload
    details = payload.get("details")
    paths = details.get("executable_test_paths") if type(details) is dict else None
    if (
        payload.get("candidate") != profile.candidate.to_value()
        or payload.get("stage") != "test_manifest"
        or payload.get("state") != "passed"
        or payload.get("outcome") is not None
        or payload.get("complete") is not True
        or type(paths) is not list
        or paths != list(profile.test_paths)
    ):
        raise RuntimeError("Luanti Lua test manifest is invalid")
    return artifact


def _load_capsule(root: Path, profile: LuaRegressionProfile) -> StableArtifact:
    artifact = _load(root, profile.fixed_capsule_reference)
    expected = {
        "candidate": profile.candidate.to_value(),
        "profile_id": profile.profile_id,
        "source_tree_path": profile.fixed_source_tree_path,
        "support_paths": list(profile.support_paths),
        "test_paths": list(profile.test_paths),
    }
    if (
        artifact.inputs != (profile.source_packet_reference,)
        or artifact.payload != expected
    ):
        raise RuntimeError("Luanti Lua fixed capsule is invalid")
    tree = root / PurePosixPath(profile.fixed_source_tree_path)
    _ordinary_directory(tree, root, "Luanti Lua fixed capsule path is unsafe")
    for relative in (
        *profile.production_paths,
        *profile.support_paths,
        *profile.test_paths,
    ):
        _ordinary_file(tree / PurePosixPath(relative), tree)
    return artifact


def _load_attempts(
    root: Path,
    profile: LuaRegressionProfile,
    capsule: ArtifactReference,
) -> list[StableArtifact]:
    artifacts: list[StableArtifact] = []
    for phase in ("f2p", "p2p"):
        for variant in ("fixed", "buggy"):
            for repetition in range(1, 4):
                artifact = _load(
                    root,
                    _attempt_reference(profile, phase, variant, repetition),
                )
                _validate_attempt(
                    profile,
                    artifact,
                    capsule=capsule,
                    phase=phase,
                    variant=variant,
                    repetition=repetition,
                )
                artifacts.append(artifact)
    return artifacts


def _validate_attempt(
    profile: LuaRegressionProfile,
    artifact: StableArtifact,
    *,
    capsule: ArtifactReference,
    phase: str,
    variant: str,
    repetition: int,
) -> None:
    payload = artifact.payload
    expected_outcome = _expected_outcome(profile, phase, variant)
    selector = profile.f2p_selector if phase == "f2p" else profile.p2p_selector
    identity = {
        "candidate": profile.candidate.to_value(),
        "profile_id": profile.profile_id,
        "phase": phase,
        "variant": variant,
        "repetition": repetition,
        "selector": selector,
        "test_path": profile.test_paths[0],
        "expected_outcome": expected_outcome,
    }
    if (
        artifact.inputs != (capsule,)
        or set(payload) != _ATTEMPT_KEYS
        or any(payload.get(key) != value for key, value in identity.items())
        or payload.get("observed_outcome") != expected_outcome
        or payload.get("matched_expected") is not True
        or payload.get("output_encoding") != "base64"
        or type(payload.get("stdout_truncated")) is not bool
        or type(payload.get("stderr_truncated")) is not bool
        or not _coherent_process_result(payload, expected_outcome)
    ):
        raise RuntimeError("Luanti Lua attempt result is invalid")
    for key in ("stdout_base64", "stderr_base64"):
        value = payload.get(key)
        if type(value) is not str:
            raise RuntimeError("Luanti Lua attempt output is invalid")
        try:
            base64.b64decode(value, validate=True)
        except ValueError as error:
            raise RuntimeError("Luanti Lua attempt output is invalid") from error


def _expected_outcome(profile: LuaRegressionProfile, phase: str, variant: str) -> str:
    if phase == "f2p" and variant == "buggy":
        return profile.f2p_buggy_outcome
    return "pass"


def _coherent_process_result(payload: object, outcome: str) -> bool:
    if type(payload) is not dict:
        return False
    if outcome == "timed_out":
        return (
            payload.get("process_outcome") == "timed_out"
            and (
                payload.get("returncode") is None
                or (
                    type(payload.get("returncode")) is int and payload["returncode"] < 0
                )
            )
            and payload.get("completed_test_count") is None
        )
    if payload.get("process_outcome") != "completed":
        return False
    returncode = payload.get("returncode")
    return (
        type(returncode) is int
        and payload.get("completed_test_count") == 1
        and (returncode == 0 if outcome == "pass" else returncode != 0)
    )


def _load_aggregate(
    root: Path,
    profile: LuaRegressionProfile,
    attempts: tuple[StableArtifact, ...],
) -> StableArtifact:
    artifact = _load(root, _aggregate_reference(profile))
    expected_cells = []
    for phase, variant in sorted(
        ((phase, variant) for phase in ("f2p", "p2p") for variant in ("fixed", "buggy"))
    ):
        expected_cells.append(
            {
                "phase": phase,
                "variant": variant,
                "observed_outcomes": [_expected_outcome(profile, phase, variant)] * 3,
            }
        )
    expected = {
        "candidate": profile.candidate.to_value(),
        "profile_id": profile.profile_id,
        "repetitions_per_cell": 3,
        "attempt_count": 12,
        "stable_repetitions": True,
        "all_expected": True,
        "qualified": True,
        "cells": expected_cells,
    }
    if (
        set(artifact.payload) != _AGGREGATE_KEYS
        or artifact.payload != expected
        or artifact.inputs != tuple(item.reference for item in attempts)
    ):
        raise RuntimeError("Luanti Lua regression aggregate is invalid")
    return artifact


def _load_transition(
    root: Path,
    profile: LuaRegressionProfile,
    *,
    manifest: ArtifactReference,
    capsule: ArtifactReference,
    attempts: tuple[ArtifactReference, ...],
    aggregate: ArtifactReference,
) -> StableArtifact:
    artifact = _load(root, _transition_reference(profile))
    expected = {
        "candidate": profile.candidate.to_value(),
        "profile_id": profile.profile_id,
        "repetitions_per_cell": 3,
        "attempt_count": 12,
        "stable_repetitions": True,
        "all_expected": True,
        "qualified": True,
        "truth_table": {
            "fixed": {"f2p": True, "p2p": True},
            "buggy": {"f2p": False, "p2p": True},
        },
        "complete": True,
    }
    if (
        set(artifact.payload) != _TRANSITION_KEYS
        or artifact.payload != expected
        or artifact.inputs != (manifest, capsule, *attempts, aggregate)
    ):
        raise RuntimeError("Luanti Lua regression transition is invalid")
    return artifact


def _load_test_repetitions_stage(
    root: Path,
    profile: LuaRegressionProfile,
    *,
    manifest: ArtifactReference,
    transition: ArtifactReference,
) -> StableArtifact:
    reference = ArtifactReference(
        (
            f"candidate_stage/luanti/{profile.candidate.change_number}/"
            "test_repetitions/v2"
        ),
        "candidate_stage",
        "2.0",
    )
    artifact = _load(root, reference)
    payload = artifact.payload
    details = payload.get("details")
    evidence = details.get("evidence_artifacts") if type(details) is dict else None
    if (
        artifact.inputs != (manifest, transition)
        or payload.get("candidate") != profile.candidate.to_value()
        or payload.get("stage") != "test_repetitions"
        or payload.get("state") != "passed"
        or payload.get("outcome") is not None
        or payload.get("complete") is not True
        or type(details) is not dict
        or details.get("next_gate") != "version_materialization"
        or evidence != [_reference_value(transition)]
    ):
        raise RuntimeError("Luanti Lua test repetition stage is invalid")
    return artifact


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _reference_value(reference: ArtifactReference) -> dict[str, str]:
    return {
        "artifact_id": reference.artifact_id,
        "artifact_kind": reference.artifact_kind,
        "semantic_version": reference.semantic_version,
    }


def _ordinary_root(root: Path) -> Path:
    try:
        info = root.lstat()
        resolved = root.resolve(strict=True)
    except FileNotFoundError as error:
        raise RuntimeError("artifact root is missing") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("artifact root is unsafe")
    return resolved


def _ordinary_directory(path: Path, root: Path, message: str) -> None:
    _require_under(path, root, message)
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError as error:
            raise RuntimeError(message) from error
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError(message)


def _ordinary_file(path: Path, root: Path) -> None:
    _require_under(path, root, "Luanti Lua fixed capsule path is unsafe")
    current = root
    parts = path.relative_to(root).parts
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError as error:
            raise RuntimeError("Luanti Lua fixed capsule path is unsafe") from error
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError("Luanti Lua fixed capsule path is unsafe")
        if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("Luanti Lua fixed capsule path is unsafe")
        if index == len(parts) - 1 and (
            not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
        ):
            raise RuntimeError("Luanti Lua fixed capsule path is unsafe")


def _require_under(path: Path, root: Path, message: str) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise RuntimeError(message) from error
    if not relative.parts or any(part in {".", ".."} for part in relative.parts):
        raise RuntimeError(message)


__all__ = [
    "LuaRuntimeVisualClosureBlocker",
    "REQUIRED_RUNTIME_VISUAL_EVIDENCE",
    "evaluate_luanti_lua_version_materialization",
]
