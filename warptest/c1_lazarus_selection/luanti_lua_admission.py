"""Bridge closed Luanti Lua regression evidence into the formal v2 DAG."""

from __future__ import annotations

import base64
import binascii
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal, Mapping

from .epoch import SELECTION_EPOCH_V2
from .models import CandidateKey
from .operations import evaluate_test_manifest
from .pipeline import (
    StageDecision,
    V2PreannotationBatchContext,
    run_preannotation_pipeline,
)
from .source_atomicity import (
    SourceAtomicityReceipt,
    load_source_atomicity_artifact,
    load_source_atomicity_batch_context,
)
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_V2 = "2.0"
_V3 = "3.0"
_ATTEMPT_FIELDS = frozenset(
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
_AGGREGATE_FIELDS = frozenset(
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
_TRANSITION_FIELDS = frozenset(
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
_CELL_ORDER = tuple(
    (phase, variant) for phase in ("f2p", "p2p") for variant in ("fixed", "buggy")
)


@dataclass(frozen=True)
class LuantiLuaAdmissionProjection:
    """The exact formal boundary reached by one local regression aggregate."""

    candidate: CandidateKey
    manifest: StableArtifact
    transition: StableArtifact
    stage: StableArtifact
    checkpoint: StableArtifact
    qualified: bool
    next_gate: Literal["version_materialization"] | None
    resumed: bool


def reproject_qualified_luanti_lua_admission(
    artifact_root: str | Path,
    *,
    candidate: CandidateKey,
    authorization: AuthorizationRecord,
) -> LuantiLuaAdmissionProjection:
    """Restore the qualified gate-1--7 boundary after a downstream terminal.

    The method is intentionally narrower than the normal DAG runner.  It
    strictly reloads source, atomicity, manifest, capsule, all twelve attempts,
    the aggregate and its v2 transition.  Only the already-qualified
    ``test_repetitions`` stage and active checkpoint become current heads.
    Older source or downstream terminal revisions remain auditable history and
    no version, replay, oracle, golden or construction result is created.
    """

    if type(candidate) is not CandidateKey or candidate.repository_key != "luanti":
        raise ValueError("Lua re-projection candidate must be typed and registered")
    if type(authorization) is not AuthorizationRecord:
        raise ValueError("Lua re-projection authorization is invalid")
    if (
        authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("Lua re-projection authorization is inactive")
    root = Path(artifact_root).resolve(strict=True)
    atomicity_context = load_source_atomicity_batch_context(
        root,
        repository_key=candidate.repository_key,
    )
    batch_context = V2PreannotationBatchContext.from_source_atomicity(atomicity_context)
    source, patch = _load_screening_prefix(root, candidate)
    atomicity = load_source_atomicity_artifact(
        batch_context=atomicity_context,
        candidate=candidate,
    )
    if atomicity.outcome != "accepted" or atomicity.primary_issue_number is None:
        raise RuntimeError("current v2 source atomicity is not accepted")
    manifest, rebound = _load_or_rebind_manifest(
        root,
        candidate=candidate,
        patch=patch,
        atomicity=atomicity,
        batch_context=batch_context,
        authorization=authorization,
    )
    capsule = _load_capsule(root, candidate, source, manifest)
    attempts = _load_attempts(root, candidate, capsule)
    aggregate = _load_aggregate(root, candidate, capsule, attempts)
    truth_table = _truth_table(attempts)
    transition, transition_resumed = _load_or_write_transition(
        root,
        candidate=candidate,
        manifest=manifest,
        capsule=capsule,
        attempts=attempts,
        aggregate=aggregate,
        truth_table=truth_table,
        authorization=authorization,
    )
    expected_truth = {
        "fixed": {"f2p": True, "p2p": True},
        "buggy": {"f2p": False, "p2p": True},
    }
    if (
        transition.payload["qualified"] is not True
        or transition.payload["stable_repetitions"] is not True
        or transition.payload["all_expected"] is not True
        or transition.payload["attempt_count"] != 12
        or transition.payload["truth_table"] != expected_truth
    ):
        raise RuntimeError("Lua re-projection truth table is not qualified")
    details = _stage_details(transition, attempts)
    stage_reference = ArtifactReference(
        f"candidate_stage/luanti/{candidate.change_number}/test_repetitions/v2",
        "candidate_stage",
        _V2,
    )
    stage_payload = {
        "candidate": candidate.to_value(),
        "stage": "test_repetitions",
        "state": "passed",
        "outcome": None,
        "details": details,
        "complete": True,
    }
    created_at = _now()
    write_stable_artifact(
        root,
        artifact_id=stage_reference.artifact_id,
        artifact_kind=stage_reference.artifact_kind,
        semantic_version=stage_reference.semantic_version,
        created_at=created_at,
        inputs=(manifest.reference, transition.reference),
        payload=stage_payload,
        authorization=authorization,
    )
    stage = _load(
        root,
        stage_reference.artifact_id,
        stage_reference.artifact_kind,
        stage_reference.semantic_version,
    )
    if (
        stage.inputs != (manifest.reference, transition.reference)
        or stage.payload != stage_payload
    ):
        raise RuntimeError("Lua re-projection test stage reload is invalid")
    checkpoint_reference = ArtifactReference(
        f"preannotation_checkpoint/luanti/{candidate.change_number}/current/v2",
        "preannotation_checkpoint",
        _V2,
    )
    checkpoint_payload = {
        "candidate": candidate.to_value(),
        "completed_stages": ["test_manifest", "test_repetitions"],
        "state": "active",
        "outcome": None,
        "current_artifact_id": stage.reference.artifact_id,
    }
    write_stable_artifact(
        root,
        artifact_id=checkpoint_reference.artifact_id,
        artifact_kind=checkpoint_reference.artifact_kind,
        semantic_version=checkpoint_reference.semantic_version,
        created_at=created_at,
        inputs=(stage.reference,),
        payload=checkpoint_payload,
        authorization=authorization,
    )
    checkpoint = _load(
        root,
        checkpoint_reference.artifact_id,
        checkpoint_reference.artifact_kind,
        checkpoint_reference.semantic_version,
    )
    if (
        checkpoint.inputs != (stage.reference,)
        or checkpoint.payload != checkpoint_payload
    ):
        raise RuntimeError("Lua re-projection checkpoint reload is invalid")
    return LuantiLuaAdmissionProjection(
        candidate=candidate,
        manifest=manifest,
        transition=transition,
        stage=stage,
        checkpoint=checkpoint,
        qualified=True,
        next_gate="version_materialization",
        resumed=transition_resumed and not rebound,
    )


def project_luanti_lua_regression_to_v2(
    artifact_root: str | Path,
    *,
    candidate: CandidateKey,
    authorization: AuthorizationRecord,
) -> LuantiLuaAdmissionProjection:
    """Project a fully reloaded v3 truth table, stopping before materialization.

    This function performs no candidate execution.  It reloads the current v2
    source/screening/manifest prefix, the v2 fixed capsule, every v3 process
    attempt, and the v3 aggregate.  A v2 transition receipt makes that evidence
    composable with the formal pre-annotation pipeline.  The pipeline then
    writes only ``test_repetitions/v2`` and its active or terminal checkpoint.
    A qualified projection remains active at ``version_materialization``;
    capsule evidence is deliberately not treated as a complete materialized
    version, replay, oracle, construction, or annotation-eligibility result.
    """

    if type(candidate) is not CandidateKey or candidate.repository_key != "luanti":
        raise ValueError("Lua admission candidate must be a typed Luanti candidate")
    if type(authorization) is not AuthorizationRecord:
        raise ValueError("Lua admission authorization is invalid")
    if (
        authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("Lua admission authorization is inactive")
    root = Path(artifact_root).resolve(strict=True)

    atomicity_context = load_source_atomicity_batch_context(
        root,
        repository_key=candidate.repository_key,
    )
    batch_context = V2PreannotationBatchContext.from_source_atomicity(atomicity_context)
    source, patch = _load_screening_prefix(root, candidate)
    atomicity = load_source_atomicity_artifact(
        batch_context=atomicity_context,
        candidate=candidate,
    )
    if atomicity.outcome != "accepted" or atomicity.primary_issue_number is None:
        raise RuntimeError("current v2 source atomicity is not accepted")
    manifest, manifest_rebound = _load_or_rebind_manifest(
        root,
        candidate=candidate,
        patch=patch,
        atomicity=atomicity,
        batch_context=batch_context,
        authorization=authorization,
    )
    _load_manifest_checkpoint(root, candidate, manifest)
    capsule = _load_capsule(root, candidate, source, manifest)
    attempts = _load_attempts(root, candidate, capsule)
    aggregate = _load_aggregate(root, candidate, capsule, attempts)
    truth_table = _truth_table(attempts)
    transition, transition_resumed = _load_or_write_transition(
        root,
        candidate=candidate,
        manifest=manifest,
        capsule=capsule,
        attempts=attempts,
        aggregate=aggregate,
        truth_table=truth_table,
        authorization=authorization,
    )
    details = _stage_details(transition, attempts)

    def unexpected_manifest(_candidate: CandidateKey) -> StageDecision:
        raise RuntimeError("current v2 test manifest checkpoint is missing")

    def project_repetitions(_candidate: CandidateKey) -> StageDecision:
        if transition.payload["qualified"] is True:
            return StageDecision.passed(details)
        outcome = (
            "flake_observed"
            if transition.payload["stable_repetitions"] is False
            else "quality_rejected"
        )
        return StageDecision.terminal(outcome, details)

    pipeline_result = run_preannotation_pipeline(
        root,
        candidates=(candidate,),
        operations={
            "test_manifest": unexpected_manifest,
            "test_repetitions": project_repetitions,
        },
        authorization=authorization,
        selection_epoch=SELECTION_EPOCH_V2,
        through_stage="test_repetitions",
        test_workers=1,
        build_workers=1,
        replay_workers=1,
        persistence_workers=1,
        reload_workers=1,
        queue_limit=1,
        batch_contexts={candidate.repository_key: batch_context},
    )
    qualified = transition.payload["qualified"] is True
    if qualified:
        if pipeline_result.active != 1 or pipeline_result.terminal_counts:
            raise RuntimeError("qualified Lua admission did not remain active")
    elif pipeline_result.terminal_counts not in (
        {"quality_rejected": 1},
        {"flake_observed": 1},
    ):
        raise RuntimeError("unqualified Lua admission did not terminate")
    stage, checkpoint = _load_formal_projection(
        root,
        candidate=candidate,
        manifest=manifest,
        transition=transition,
        details=details,
        qualified=qualified,
    )
    return LuantiLuaAdmissionProjection(
        candidate=candidate,
        manifest=manifest,
        transition=transition,
        stage=stage,
        checkpoint=checkpoint,
        qualified=qualified,
        next_gate="version_materialization" if qualified else None,
        resumed=(
            transition_resumed and pipeline_result.resumed == 1 and not manifest_rebound
        ),
    )


def _load_screening_prefix(
    root: Path,
    candidate: CandidateKey,
) -> tuple[StableArtifact, StableArtifact]:
    source = _load(
        root,
        f"candidate_source_packet/luanti/{candidate.change_number}/source/v2",
        "candidate_source_packet",
        _V2,
    )
    patch = _load(
        root,
        f"candidate_test_patch/luanti/{candidate.change_number}/screening/v2",
        "candidate_test_patch",
        _V2,
    )
    if patch.inputs[-1:] != (source.reference,):
        raise RuntimeError("current v2 screening prefix is invalid")
    return source, patch


def _load_or_rebind_manifest(
    root: Path,
    *,
    candidate: CandidateKey,
    patch: StableArtifact,
    atomicity: SourceAtomicityReceipt,
    batch_context: V2PreannotationBatchContext,
    authorization: AuthorizationRecord,
) -> tuple[StableArtifact, bool]:
    recomputed = evaluate_test_manifest(
        root,
        candidate,
        selection_epoch=SELECTION_EPOCH_V2,
        batch_context=batch_context,
    )
    if recomputed.state != "passed":
        raise RuntimeError("current v2 test manifest no longer passes")
    manifest = _load(
        root,
        f"candidate_stage/luanti/{candidate.change_number}/test_manifest/v2",
        "candidate_stage",
        _V2,
    )
    expected_payload = {
        "candidate": candidate.to_value(),
        "stage": "test_manifest",
        "state": "passed",
        "outcome": None,
        "details": dict(recomputed.details),
        "complete": True,
    }
    if manifest.payload != expected_payload:
        raise RuntimeError("current v2 test manifest payload is invalid")
    formal_inputs = (patch.reference, atomicity.artifact.reference)
    rebound = False
    if manifest.inputs == (patch.reference,):
        write_stable_artifact(
            root,
            artifact_id=manifest.reference.artifact_id,
            artifact_kind=manifest.reference.artifact_kind,
            semantic_version=manifest.reference.semantic_version,
            created_at=_now(),
            inputs=formal_inputs,
            payload=expected_payload,
            authorization=authorization,
        )
        manifest = _load(
            root,
            f"candidate_stage/luanti/{candidate.change_number}/test_manifest/v2",
            "candidate_stage",
            _V2,
        )
        rebound = True
    if manifest.inputs != formal_inputs or manifest.payload != expected_payload:
        raise RuntimeError("current v2 test manifest binding is invalid")
    return manifest, rebound


def _load_manifest_checkpoint(
    root: Path,
    candidate: CandidateKey,
    manifest: StableArtifact,
) -> StableArtifact:
    checkpoint = _load(
        root,
        f"preannotation_checkpoint/luanti/{candidate.change_number}/current/v2",
        "preannotation_checkpoint",
        _V2,
    )
    completed = checkpoint.payload.get("completed_stages")
    if completed == ["test_manifest"]:
        if checkpoint.inputs != (manifest.reference,) or checkpoint.payload != {
            "candidate": candidate.to_value(),
            "completed_stages": ["test_manifest"],
            "state": "active",
            "outcome": None,
            "current_artifact_id": manifest.reference.artifact_id,
        }:
            raise RuntimeError("current v2 manifest checkpoint is invalid")
    elif completed == ["test_manifest", "test_repetitions"]:
        stage_reference = ArtifactReference(
            f"candidate_stage/luanti/{candidate.change_number}/test_repetitions/v2",
            "candidate_stage",
            _V2,
        )
        if (
            checkpoint.inputs != (stage_reference,)
            or checkpoint.payload.get("state") not in {"active", "terminal"}
            or checkpoint.payload.get("current_artifact_id")
            != stage_reference.artifact_id
        ):
            raise RuntimeError("current v2 repetitions checkpoint is invalid")
    else:
        raise RuntimeError(
            "Lua admission checkpoint is outside its projection boundary"
        )
    return checkpoint


def _load_capsule(
    root: Path,
    candidate: CandidateKey,
    source: StableArtifact,
    manifest: StableArtifact,
) -> StableArtifact:
    capsule = _load(
        root,
        f"fixed_source_capsule/luanti/{candidate.change_number}/fixed/v2",
        "fixed_source_capsule",
        _V2,
    )
    payload = capsule.payload
    if capsule.inputs != (source.reference,):
        raise RuntimeError("fixed capsule source prerequisite is invalid")
    if set(payload) != {
        "candidate",
        "profile_id",
        "source_tree_path",
        "support_paths",
        "test_paths",
    }:
        raise RuntimeError("fixed capsule schema is invalid")
    manifest_paths = manifest.payload["details"]["executable_test_paths"]
    if (
        payload["candidate"] != candidate.to_value()
        or type(payload["profile_id"]) is not str
        or not payload["profile_id"]
        or payload["test_paths"] != manifest_paths
        or not _string_paths(payload["support_paths"], allow_empty=True)
    ):
        raise RuntimeError("fixed capsule binding is invalid")
    relative = _relative_path(payload["source_tree_path"], "fixed capsule tree")
    required_prefix = PurePosixPath(
        "private", "fixed-capsules", "luanti", str(candidate.change_number)
    )
    if relative.parts[: len(required_prefix.parts)] != required_prefix.parts:
        raise RuntimeError("fixed capsule tree is outside its candidate namespace")
    tree = _ordinary_directory(root.joinpath(*relative.parts), root)
    required_paths = (
        *manifest_paths,
        *payload["support_paths"],
        *_production_paths(root, candidate),
    )
    if len(required_paths) != len(set(required_paths)):
        raise RuntimeError("fixed capsule file inventory is duplicated")
    for value in required_paths:
        path = _relative_path(value, "fixed capsule file")
        _ordinary_file(tree.joinpath(*path.parts), tree)
    return capsule


def _production_paths(root: Path, candidate: CandidateKey) -> tuple[str, ...]:
    patch = _load(
        root,
        f"candidate_test_patch/luanti/{candidate.change_number}/screening/v2",
        "candidate_test_patch",
        _V2,
    )
    paths = patch.payload.get("production_paths")
    if not _string_paths(paths, allow_empty=False):
        raise RuntimeError("current v2 production inventory is invalid")
    return tuple(paths)


def _load_attempts(
    root: Path,
    candidate: CandidateKey,
    capsule: StableArtifact,
) -> tuple[StableArtifact, ...]:
    attempts: list[StableArtifact] = []
    for phase, variant in _CELL_ORDER:
        for repetition in (1, 2, 3):
            attempt = _load(
                root,
                (
                    f"lua_regression_attempt/luanti/{candidate.change_number}/"
                    f"{phase}/{variant}/{repetition}/v3"
                ),
                "lua_regression_attempt",
                _V3,
            )
            _validate_attempt(
                attempt,
                candidate=candidate,
                capsule=capsule,
                phase=phase,
                variant=variant,
                repetition=repetition,
            )
            attempts.append(attempt)
    return tuple(attempts)


def _validate_attempt(
    attempt: StableArtifact,
    *,
    candidate: CandidateKey,
    capsule: StableArtifact,
    phase: str,
    variant: str,
    repetition: int,
) -> None:
    payload = attempt.payload
    if attempt.inputs != (capsule.reference,) or set(payload) != _ATTEMPT_FIELDS:
        raise RuntimeError("Lua regression attempt dependency or schema is invalid")
    expected_outcome = payload["expected_outcome"]
    observed_outcome = payload["observed_outcome"]
    if (
        payload["candidate"] != candidate.to_value()
        or payload["profile_id"] != capsule.payload["profile_id"]
        or payload["phase"] != phase
        or payload["variant"] != variant
        or payload["repetition"] != repetition
        or type(payload["selector"]) is not str
        or not payload["selector"]
        or payload["test_path"] not in capsule.payload["test_paths"]
        or expected_outcome not in {"pass", "failure", "timed_out"}
        or observed_outcome not in {"pass", "failure", "timed_out"}
        or payload["matched_expected"] is not (observed_outcome == expected_outcome)
    ):
        raise RuntimeError("Lua regression attempt identity or outcome is invalid")
    if variant == "fixed" or phase == "p2p":
        if expected_outcome != "pass":
            raise RuntimeError("Lua regression expected truth table is invalid")
    elif expected_outcome not in {"failure", "timed_out"}:
        raise RuntimeError("Lua regression expected truth table is invalid")
    if observed_outcome == "timed_out":
        valid_process = (
            payload["process_outcome"] == "timed_out"
            and (payload["returncode"] is None or type(payload["returncode"]) is int)
            and payload["completed_test_count"] is None
        )
    elif observed_outcome == "pass":
        valid_process = (
            payload["process_outcome"],
            payload["returncode"],
            payload["completed_test_count"],
        ) == ("completed", 0, 1)
    else:
        valid_process = (
            payload["process_outcome"] == "completed"
            and type(payload["returncode"]) is int
            and payload["returncode"] != 0
            and payload["completed_test_count"] == 1
        )
    if not valid_process:
        raise RuntimeError("Lua regression attempt process result is invalid")
    if (
        payload["output_encoding"] != "base64"
        or type(payload["stdout_truncated"]) is not bool
        or type(payload["stderr_truncated"]) is not bool
    ):
        raise RuntimeError("Lua regression attempt output metadata is invalid")
    for field in ("stdout_base64", "stderr_base64"):
        if type(payload[field]) is not str:
            raise RuntimeError("Lua regression attempt output is invalid")
        try:
            base64.b64decode(payload[field], validate=True)
        except (ValueError, binascii.Error) as error:
            raise RuntimeError("Lua regression attempt output is invalid") from error


def _load_aggregate(
    root: Path,
    candidate: CandidateKey,
    capsule: StableArtifact,
    attempts: tuple[StableArtifact, ...],
) -> StableArtifact:
    aggregate = _load(
        root,
        f"lua_regression_aggregate/luanti/{candidate.change_number}/fixed-buggy/v3",
        "lua_regression_aggregate",
        _V3,
    )
    payload = aggregate.payload
    references = tuple(attempt.reference for attempt in attempts)
    if aggregate.inputs != references:
        raise RuntimeError("Lua regression aggregate attempt closure is invalid")
    if set(payload) != _AGGREGATE_FIELDS:
        raise RuntimeError("Lua regression aggregate schema is invalid")
    groups = _attempt_groups(attempts)
    stable = all(
        len({item.payload["observed_outcome"] for item in group}) == 1
        for group in groups.values()
    )
    all_expected = all(item.payload["matched_expected"] is True for item in attempts)
    cells = [
        {
            "phase": phase,
            "variant": variant,
            "observed_outcomes": [
                item.payload["observed_outcome"] for item in groups[(phase, variant)]
            ],
        }
        for phase, variant in sorted(_CELL_ORDER)
    ]
    if (
        payload["candidate"] != candidate.to_value()
        or payload["profile_id"] != capsule.payload["profile_id"]
        or payload["repetitions_per_cell"] != 3
        or payload["attempt_count"] != 12
        or payload["stable_repetitions"] is not stable
        or payload["all_expected"] is not all_expected
        or payload["qualified"] is not (stable and all_expected)
        or payload["cells"] != cells
    ):
        raise RuntimeError("Lua regression aggregate binding is invalid")
    return aggregate


def _attempt_groups(
    attempts: tuple[StableArtifact, ...],
) -> dict[tuple[str, str], tuple[StableArtifact, ...]]:
    groups: dict[tuple[str, str], list[StableArtifact]] = {}
    for attempt in attempts:
        key = (attempt.payload["phase"], attempt.payload["variant"])
        groups.setdefault(key, []).append(attempt)
    if set(groups) != set(_CELL_ORDER) or any(
        len(group) != 3 for group in groups.values()
    ):
        raise RuntimeError("Lua regression attempt coverage is invalid")
    return {
        key: tuple(sorted(group, key=lambda item: item.payload["repetition"]))
        for key, group in groups.items()
    }


def _truth_table(
    attempts: tuple[StableArtifact, ...],
) -> dict[str, dict[str, bool]]:
    groups = _attempt_groups(attempts)
    return {
        variant: {
            phase: all(
                item.payload["observed_outcome"] == "pass"
                for item in groups[(phase, variant)]
            )
            for phase in ("f2p", "p2p")
        }
        for variant in ("fixed", "buggy")
    }


def _transition_reference(candidate: CandidateKey) -> ArtifactReference:
    return ArtifactReference(
        (
            f"luanti_lua_regression_transition/luanti/{candidate.change_number}/"
            "test-repetitions/v2"
        ),
        "luanti_lua_regression_transition",
        _V2,
    )


def _load_or_write_transition(
    root: Path,
    *,
    candidate: CandidateKey,
    manifest: StableArtifact,
    capsule: StableArtifact,
    attempts: tuple[StableArtifact, ...],
    aggregate: StableArtifact,
    truth_table: dict[str, dict[str, bool]],
    authorization: AuthorizationRecord,
) -> tuple[StableArtifact, bool]:
    reference = _transition_reference(candidate)
    inputs = (
        manifest.reference,
        capsule.reference,
        *(attempt.reference for attempt in attempts),
        aggregate.reference,
    )
    payload = {
        "candidate": candidate.to_value(),
        "profile_id": aggregate.payload["profile_id"],
        "repetitions_per_cell": 3,
        "attempt_count": 12,
        "stable_repetitions": aggregate.payload["stable_repetitions"],
        "all_expected": aggregate.payload["all_expected"],
        "qualified": aggregate.payload["qualified"],
        "truth_table": truth_table,
        "complete": True,
    }
    try:
        transition = _load(
            root,
            reference.artifact_id,
            reference.artifact_kind,
            reference.semantic_version,
        )
    except RuntimeError as error:
        if str(error) not in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            raise
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
        transition = _load(
            root,
            reference.artifact_id,
            reference.artifact_kind,
            reference.semantic_version,
        )
        resumed = False
    else:
        resumed = True
    if (
        set(transition.payload) != _TRANSITION_FIELDS
        or transition.inputs != inputs
        or transition.payload != payload
    ):
        raise RuntimeError("Lua regression v2 transition binding is invalid")
    return transition, resumed


def _stage_details(
    transition: StableArtifact,
    attempts: tuple[StableArtifact, ...],
) -> dict[str, object]:
    groups = _attempt_groups(attempts)
    pass_count = lambda phase, variant: sum(  # noqa: E731
        item.payload["observed_outcome"] == "pass" for item in groups[(phase, variant)]
    )
    return {
        "profile_id": transition.payload["profile_id"],
        "repetitions_per_cell": 3,
        "fixed_f2p_passes": pass_count("f2p", "fixed"),
        "buggy_f2p_failures": 3 - pass_count("f2p", "buggy"),
        "fixed_p2p_passes": pass_count("p2p", "fixed"),
        "buggy_p2p_passes": pass_count("p2p", "buggy"),
        "next_gate": (
            "version_materialization"
            if transition.payload["qualified"] is True
            else None
        ),
        "evidence_artifacts": [
            {
                "artifact_id": transition.reference.artifact_id,
                "artifact_kind": transition.reference.artifact_kind,
                "semantic_version": transition.reference.semantic_version,
            }
        ],
    }


def _load_formal_projection(
    root: Path,
    *,
    candidate: CandidateKey,
    manifest: StableArtifact,
    transition: StableArtifact,
    details: Mapping[str, object],
    qualified: bool,
) -> tuple[StableArtifact, StableArtifact]:
    stage = _load(
        root,
        f"candidate_stage/luanti/{candidate.change_number}/test_repetitions/v2",
        "candidate_stage",
        _V2,
    )
    expected_state = "passed" if qualified else "terminal"
    expected_outcome = (
        None
        if qualified
        else (
            "flake_observed"
            if transition.payload["stable_repetitions"] is False
            else "quality_rejected"
        )
    )
    if stage.inputs != (manifest.reference, transition.reference) or stage.payload != {
        "candidate": candidate.to_value(),
        "stage": "test_repetitions",
        "state": expected_state,
        "outcome": expected_outcome,
        "details": dict(details),
        "complete": True,
    }:
        raise RuntimeError("formal v2 test repetitions projection is invalid")
    checkpoint = _load(
        root,
        f"preannotation_checkpoint/luanti/{candidate.change_number}/current/v2",
        "preannotation_checkpoint",
        _V2,
    )
    expected_checkpoint = {
        "candidate": candidate.to_value(),
        "completed_stages": ["test_manifest", "test_repetitions"],
        "state": "active" if qualified else "terminal",
        "outcome": expected_outcome,
        "current_artifact_id": stage.reference.artifact_id,
    }
    if (
        checkpoint.inputs != (stage.reference,)
        or checkpoint.payload != expected_checkpoint
    ):
        raise RuntimeError("formal v2 test repetitions checkpoint is invalid")
    return stage, checkpoint


def _load(
    root: Path,
    artifact_id: str,
    artifact_kind: str,
    semantic_version: str,
) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=artifact_id,
        artifact_kind=artifact_kind,
        semantic_version=semantic_version,
    )


def _string_paths(value: object, *, allow_empty: bool) -> bool:
    return (
        type(value) is list
        and (allow_empty or bool(value))
        and all(
            type(item) is str and bool(item) and _is_relative_path(item)
            for item in value
        )
    )


def _is_relative_path(value: str) -> bool:
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and bool(path.parts)
        and "." not in path.parts
        and ".." not in path.parts
        and path.as_posix() == value
    )


def _relative_path(value: object, label: str) -> PurePosixPath:
    if type(value) is not str or not _is_relative_path(value):
        raise RuntimeError(f"{label} path is unsafe")
    return PurePosixPath(value)


def _ordinary_directory(path: Path, parent: Path) -> Path:
    resolved_parent = parent.resolve(strict=True)
    candidate = path.absolute()
    info = candidate.lstat()
    resolved = candidate.resolve(strict=True)
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or resolved_parent not in resolved.parents
    ):
        raise RuntimeError("fixed capsule tree is unsafe")
    return resolved


def _ordinary_file(path: Path, parent: Path) -> Path:
    resolved_parent = parent.resolve(strict=True)
    candidate = path.absolute()
    info = candidate.lstat()
    resolved = candidate.resolve(strict=True)
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or resolved_parent not in resolved.parents
    ):
        raise RuntimeError("fixed capsule file is unsafe")
    return resolved


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = [
    "LuantiLuaAdmissionProjection",
    "project_luanti_lua_regression_to_v2",
    "reproject_qualified_luanti_lua_admission",
]
