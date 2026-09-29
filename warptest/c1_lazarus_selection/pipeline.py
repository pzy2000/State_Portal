"""Bounded candidate-level pre-annotation DAG."""

from __future__ import annotations

import threading
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Literal, Mapping

from .epoch import (
    SELECTION_EPOCH_V1,
    SELECTION_EPOCH_V2,
    SelectionEpoch,
    require_selection_epoch,
)
from .models import CandidateKey
from .resource_guard import disk_capacity, require_write_capacity
from .screening import resolve_raw_patch_evidence
from .source_atomicity import (
    SourceAtomicityBatchContext,
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


PREANNOTATION_STAGES = (
    "test_manifest",
    "test_repetitions",
    "version_materialization",
    "frozen_replay",
    "hidden_oracle",
    "construction_gate",
    "annotation_eligibility",
)
_REPOSITORY_ORDER = ("luanti", "blender", "jupyterlab", "openra", "libreoffice")
_REPOSITORY_RANK = {name: index for index, name in enumerate(_REPOSITORY_ORDER)}
_TEST_STAGES = frozenset({"test_manifest", "test_repetitions"})
_BUILD_STAGES = frozenset({"version_materialization"})
_REPLAY_STAGES = frozenset(PREANNOTATION_STAGES) - _TEST_STAGES - _BUILD_STAGES
_TERMINAL_OUTCOMES = frozenset(
    {
        "version_incompatible",
        "flake_observed",
        "infrastructure_blocked",
        "replay_invalid",
        "quality_rejected",
    }
)


@dataclass(frozen=True)
class StageDecision:
    state: Literal["passed", "terminal", "eligible"]
    outcome: str | None
    details: Mapping[str, object]

    @classmethod
    def passed(cls, details: Mapping[str, object]) -> StageDecision:
        return cls("passed", None, dict(details))

    @classmethod
    def terminal(
        cls,
        outcome: str,
        details: Mapping[str, object] | None = None,
    ) -> StageDecision:
        return cls("terminal", outcome, {} if details is None else dict(details))

    @classmethod
    def eligible(cls) -> StageDecision:
        return cls("eligible", "qualified", {})

    def __post_init__(self) -> None:
        if type(self.details) is not dict:
            raise ValueError("stage decision details are invalid")
        if self.state == "passed":
            if self.outcome is not None:
                raise ValueError("passed stage decision has an outcome")
        elif self.state == "terminal":
            if self.outcome not in _TERMINAL_OUTCOMES:
                raise ValueError("terminal stage decision outcome is invalid")
        elif self.state == "eligible":
            if self.outcome != "qualified":
                raise ValueError("eligible stage decision is invalid")
        else:
            raise ValueError("stage decision state is invalid")


@dataclass(frozen=True)
class V2PreannotationBatchContext:
    """Immutable membership rooted in one already validated v2 enumeration."""

    artifact_root: Path
    repository_key: str
    enumeration_reference: ArtifactReference
    candidate_numbers: frozenset[int]
    source_atomicity_context: SourceAtomicityBatchContext | None = None

    @classmethod
    def from_enumeration(
        cls,
        *,
        artifact_root: str | Path,
        repository_key: str,
        enumeration_reference: ArtifactReference,
        candidate_numbers: frozenset[int],
    ) -> V2PreannotationBatchContext:
        root = Path(artifact_root).resolve(strict=True)
        expected_reference = SELECTION_EPOCH_V2.enumeration_reference(repository_key)
        if enumeration_reference != expected_reference:
            raise ValueError("v2 batch candidate repository reference is invalid")
        return cls(
            artifact_root=root,
            repository_key=repository_key,
            enumeration_reference=enumeration_reference,
            candidate_numbers=candidate_numbers,
        )

    @classmethod
    def from_source_atomicity(
        cls,
        context: SourceAtomicityBatchContext,
    ) -> V2PreannotationBatchContext:
        if type(context) is not SourceAtomicityBatchContext:
            raise TypeError("v2 source atomicity batch context is not typed")
        return cls(
            artifact_root=context.artifact_root,
            repository_key=context.repository_key,
            enumeration_reference=context.enumeration_reference,
            candidate_numbers=context.candidate_numbers,
            source_atomicity_context=context,
        )

    def __post_init__(self) -> None:
        if (
            not isinstance(self.artifact_root, Path)
            or not self.artifact_root.is_absolute()
            or self.artifact_root.resolve(strict=True) != self.artifact_root
        ):
            raise ValueError("v2 pre-annotation batch root is invalid")
        if self.enumeration_reference != SELECTION_EPOCH_V2.enumeration_reference(
            self.repository_key
        ):
            raise ValueError("v2 batch candidate repository reference is invalid")
        if type(self.candidate_numbers) is not frozenset or any(
            type(number) is not int or number <= 0 for number in self.candidate_numbers
        ):
            raise ValueError("v2 pre-annotation batch candidates are invalid")
        if self.source_atomicity_context is not None and (
            type(self.source_atomicity_context) is not SourceAtomicityBatchContext
            or self.source_atomicity_context.artifact_root != self.artifact_root
            or self.source_atomicity_context.repository_key != self.repository_key
            or self.source_atomicity_context.enumeration_reference
            != self.enumeration_reference
            or self.source_atomicity_context.candidate_numbers != self.candidate_numbers
        ):
            raise ValueError("v2 source atomicity batch context is inconsistent")


@dataclass(frozen=True)
class PreannotationPipelineResult:
    total: int
    scheduled: int
    resumed: int
    active: int
    eligible: int
    terminal_counts: Mapping[str, int]
    paused_for_disk: int


@dataclass(frozen=True)
class _CandidateResult:
    state: Literal["active", "eligible", "terminal", "paused"]
    outcome: str | None
    resumed: bool


def run_preannotation_pipeline(
    artifact_root: str | Path,
    *,
    candidates: tuple[CandidateKey, ...],
    operations: Mapping[str, Callable[[CandidateKey], StageDecision]],
    authorization: AuthorizationRecord,
    selection_epoch: SelectionEpoch = SELECTION_EPOCH_V1,
    through_stage: str | None = None,
    batch_contexts: Mapping[str, V2PreannotationBatchContext] | None = None,
    test_workers: int = 3,
    build_workers: int = 1,
    replay_workers: int = 2,
    persistence_workers: int = 2,
    reload_workers: int = 2,
    queue_limit: int = 16,
) -> PreannotationPipelineResult:
    """Advance candidates independently while preserving per-candidate order."""

    epoch = require_selection_epoch(selection_epoch)
    stages = _stage_prefix(through_stage)
    contexts = {} if batch_contexts is None else batch_contexts
    _validate_configuration(
        candidates,
        operations,
        stages,
        test_workers,
        build_workers,
        replay_workers,
        persistence_workers,
        reload_workers,
        queue_limit,
    )
    root = Path(artifact_root)
    test_slots = threading.BoundedSemaphore(test_workers)
    build_slots = threading.BoundedSemaphore(build_workers)
    replay_slots = threading.BoundedSemaphore(replay_workers)
    persistence_slots = threading.BoundedSemaphore(persistence_workers)
    reload_slots = threading.BoundedSemaphore(reload_workers)

    def run(candidate: CandidateKey) -> _CandidateResult:
        return _run_candidate(
            root,
            candidate=candidate,
            operations=operations,
            authorization=authorization,
            test_slots=test_slots,
            build_slots=build_slots,
            replay_slots=replay_slots,
            persistence_slots=persistence_slots,
            reload_slots=reload_slots,
            epoch=epoch,
            stages=stages,
            batch_context=contexts.get(candidate.repository_key),
        )

    terminal: Counter[str] = Counter()
    scheduled = 0
    resumed = 0
    active = 0
    eligible = 0
    paused_for_disk = 0
    candidate_iterator = iter(candidates)
    with ThreadPoolExecutor(max_workers=min(queue_limit, len(candidates) or 1)) as pool:
        in_flight: set[Future[_CandidateResult]] = set()

        def submit_one() -> bool:
            try:
                candidate = next(candidate_iterator)
            except StopIteration:
                return False
            in_flight.add(pool.submit(run, candidate))
            return True

        for _ in range(min(queue_limit, len(candidates))):
            submit_one()
        while in_flight:
            completed, _ = wait(in_flight, return_when=FIRST_COMPLETED)
            for future in completed:
                in_flight.remove(future)
                result = future.result()
                scheduled += not result.resumed
                resumed += result.resumed
                active += result.state == "active"
                eligible += result.state == "eligible"
                paused_for_disk += result.state == "paused"
                if result.state == "terminal":
                    if result.outcome is None:
                        raise RuntimeError("pre-annotation terminal lost its outcome")
                    terminal[result.outcome] += 1
                submit_one()
    return PreannotationPipelineResult(
        total=len(candidates),
        scheduled=scheduled,
        resumed=resumed,
        active=active,
        eligible=eligible,
        terminal_counts=dict(sorted(terminal.items())),
        paused_for_disk=paused_for_disk,
    )


def _run_candidate(
    root: Path,
    *,
    candidate: CandidateKey,
    operations: Mapping[str, Callable[[CandidateKey], StageDecision]],
    authorization: AuthorizationRecord,
    test_slots: threading.BoundedSemaphore,
    build_slots: threading.BoundedSemaphore,
    replay_slots: threading.BoundedSemaphore,
    persistence_slots: threading.BoundedSemaphore,
    reload_slots: threading.BoundedSemaphore,
    epoch: SelectionEpoch,
    stages: tuple[str, ...],
    batch_context: V2PreannotationBatchContext | None,
) -> _CandidateResult:
    patch = _load_patch(root, candidate, epoch, batch_context=batch_context)
    checkpoint = _load_checkpoint(root, candidate, epoch)
    completed: list[str] = []
    previous = patch.reference
    if checkpoint is not None:
        completed, previous = _validate_checkpoint_chain(
            root,
            candidate,
            checkpoint,
            epoch,
            batch_context=batch_context,
        )
        state = checkpoint.payload["state"]
        if state == "eligible":
            return _CandidateResult("eligible", "qualified", True)
        if state == "terminal":
            return _CandidateResult("terminal", checkpoint.payload["outcome"], True)
        if len(completed) >= len(stages):
            return _CandidateResult("active", None, True)

    for stage in stages[len(completed) :]:
        if (
            stage == "version_materialization"
            and not disk_capacity(root).may_start_build
        ):
            return _CandidateResult("paused", None, False)
        if not disk_capacity(root).may_start_candidate:
            return _CandidateResult("paused", None, False)
        semaphore = (
            test_slots
            if stage in _TEST_STAGES
            else build_slots
            if stage in _BUILD_STAGES
            else replay_slots
        )
        atomicity: ArtifactReference | None = None
        if stage == "test_manifest" and epoch.is_v2:
            atomicity = _load_accepted_source_atomicity(
                root,
                candidate,
                batch_context=batch_context,
            )
        with semaphore:
            decision = operations[stage](candidate)
        if type(decision) is not StageDecision:
            raise RuntimeError("pre-annotation operation returned an untyped decision")
        if stage == "annotation_eligibility" and decision.state != "eligible":
            if decision.state != "terminal":
                raise RuntimeError("annotation eligibility did not reach a final state")
        elif stage != "annotation_eligibility" and decision.state == "eligible":
            raise RuntimeError("pre-annotation stage became eligible too early")

        created_at = _now()
        stage_id = (
            f"candidate_stage/{candidate.repository_key}/{candidate.change_number}/"
            f"{stage}/{epoch.label}"
        )
        state = decision.state
        payload = {
            "candidate": candidate.to_value(),
            "stage": stage,
            "state": state,
            "outcome": decision.outcome,
            "details": dict(decision.details),
            "complete": True,
        }
        evidence = _evidence_references(root, decision.details, epoch)
        prerequisites = (previous, *evidence)
        if atomicity is not None:
            if atomicity in evidence:
                raise RuntimeError(
                    "v2 test manifest duplicated its source atomicity input"
                )
            prerequisites = (previous, atomicity, *evidence)
        with persistence_slots:
            require_write_capacity(root)
            write_stable_artifact(
                root,
                artifact_id=stage_id,
                artifact_kind="candidate_stage",
                semantic_version=epoch.semantic_version,
                created_at=created_at,
                inputs=prerequisites,
                payload=payload,
                authorization=authorization,
            )
        with reload_slots:
            artifact = load_stable_artifact(
                root,
                artifact_id=stage_id,
                artifact_kind="candidate_stage",
                semantic_version=epoch.semantic_version,
            )
        _validate_stage(
            root,
            artifact,
            candidate,
            stage,
            previous,
            epoch,
            batch_context=batch_context,
        )
        previous = artifact.reference
        completed.append(stage)
        checkpoint_state = (
            "terminal"
            if decision.state == "terminal"
            else "eligible"
            if decision.state == "eligible"
            else "active"
        )
        _write_checkpoint(
            root,
            candidate=candidate,
            completed=completed,
            state=checkpoint_state,
            outcome=decision.outcome,
            current=previous,
            created_at=created_at,
            authorization=authorization,
            persistence_slots=persistence_slots,
            reload_slots=reload_slots,
            epoch=epoch,
        )
        if checkpoint_state == "terminal":
            return _CandidateResult("terminal", decision.outcome, False)
        if checkpoint_state == "eligible":
            return _CandidateResult("eligible", "qualified", False)
    if len(stages) < len(PREANNOTATION_STAGES):
        return _CandidateResult("active", None, False)
    raise RuntimeError("pre-annotation pipeline ended without a final state")


def _load_patch(
    root: Path,
    candidate: CandidateKey,
    epoch: SelectionEpoch,
    *,
    batch_context: V2PreannotationBatchContext | None = None,
) -> StableArtifact:
    artifact = load_stable_artifact(
        root,
        artifact_id=(
            f"candidate_test_patch/{candidate.repository_key}/"
            f"{candidate.change_number}/screening/{epoch.label}"
        ),
        artifact_kind="candidate_test_patch",
        semantic_version=epoch.semantic_version,
    )
    if (
        artifact.payload.get("candidate") != candidate.to_value()
        or artifact.payload.get("outcome") != "patch_eligible"
        or artifact.payload.get("complete") is not True
    ):
        raise RuntimeError("candidate test patch is not eligible")
    if epoch.is_v2:
        _validate_v2_screening_root(
            root,
            candidate,
            artifact,
            epoch,
            batch_context=batch_context,
        )
    return artifact


def _validate_v2_screening_root(
    root: Path,
    candidate: CandidateKey,
    patch: StableArtifact,
    epoch: SelectionEpoch,
    *,
    batch_context: V2PreannotationBatchContext | None = None,
) -> None:
    """Refuse a v2 pre-annotation run without its exact v2 screening chain."""

    enumeration_reference = epoch.enumeration_reference(candidate.repository_key)
    if batch_context is None:
        enumeration = load_stable_artifact(
            root,
            artifact_id=enumeration_reference.artifact_id,
            artifact_kind=enumeration_reference.artifact_kind,
            semantic_version=enumeration_reference.semantic_version,
        )
        candidates = enumeration.payload.get("candidates")
        if (
            enumeration.payload.get("repository_key") != candidate.repository_key
            or enumeration.payload.get("complete") is not True
            or type(candidates) is not list
        ):
            raise RuntimeError("v2 repository enumeration is invalid")
        try:
            enumerated = tuple(CandidateKey.from_value(value) for value in candidates)
        except ValueError as error:
            raise RuntimeError(
                "v2 repository enumeration candidates are invalid"
            ) from error
        if candidate not in enumerated:
            raise RuntimeError("v2 candidate is outside the deep-screen population")
    else:
        _require_batch_candidate(root, candidate, batch_context)

    source_reference = ArtifactReference(
        artifact_id=(
            f"candidate_source_packet/{candidate.repository_key}/"
            f"{candidate.change_number}/source/{epoch.label}"
        ),
        artifact_kind="candidate_source_packet",
        semantic_version=epoch.semantic_version,
    )
    source = load_stable_artifact(
        root,
        artifact_id=source_reference.artifact_id,
        artifact_kind=source_reference.artifact_kind,
        semantic_version=source_reference.semantic_version,
    )
    if source.inputs != (enumeration_reference,):
        raise RuntimeError("v2 source packet enumeration input is invalid")
    if (
        source.payload.get("candidate") != candidate.to_value()
        or type(source.payload.get("raw_evidence_path")) is not str
        or not source.payload["raw_evidence_path"]
        or source.payload.get("complete") is not True
    ):
        raise RuntimeError("v2 source packet binding is invalid")
    raw_patch_evidence = resolve_raw_patch_evidence(
        root,
        candidate=candidate,
        source_manifest_path=source.payload["raw_evidence_path"],
    )
    if patch.inputs != (enumeration_reference, source_reference):
        raise RuntimeError("v2 test patch source prerequisite is invalid")
    if not _nonempty_string_list(
        patch.payload.get("production_paths")
    ) or not _nonempty_string_list(patch.payload.get("test_paths")):
        raise RuntimeError("v2 test patch inventory is invalid")
    if (
        patch.payload.get("source_manifest_path")
        != raw_patch_evidence.source_manifest_path
        or patch.payload.get("raw_patch_path") != raw_patch_evidence.raw_patch_path
    ):
        raise RuntimeError("v2 test patch raw evidence binding is invalid")

    screening_reference = ArtifactReference(
        artifact_id=(
            f"screening_checkpoint/{candidate.repository_key}/"
            f"{candidate.change_number}/current/{epoch.label}"
        ),
        artifact_kind="screening_checkpoint",
        semantic_version=epoch.semantic_version,
    )
    screening = load_stable_artifact(
        root,
        artifact_id=screening_reference.artifact_id,
        artifact_kind=screening_reference.artifact_kind,
        semantic_version=screening_reference.semantic_version,
    )
    if screening.inputs != (enumeration_reference, patch.reference):
        raise RuntimeError("v2 screening checkpoint patch prerequisite is invalid")
    if (
        screening.payload.get("candidate") != candidate.to_value()
        or screening.payload.get("state") != "complete"
        or screening.payload.get("outcome") != "patch_eligible"
        or screening.payload.get("stage_artifact_id") != patch.reference.artifact_id
    ):
        raise RuntimeError("v2 screening checkpoint binding is invalid")


def _nonempty_string_list(value: object) -> bool:
    return (
        type(value) is list
        and bool(value)
        and all(type(item) is str and item for item in value)
    )


def _load_checkpoint(
    root: Path,
    candidate: CandidateKey,
    epoch: SelectionEpoch,
) -> StableArtifact | None:
    try:
        return load_stable_artifact(
            root,
            artifact_id=(
                f"preannotation_checkpoint/{candidate.repository_key}/"
                f"{candidate.change_number}/current/{epoch.label}"
            ),
            artifact_kind="preannotation_checkpoint",
            semantic_version=epoch.semantic_version,
        )
    except RuntimeError as error:
        if str(error) in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            return None
        raise


def _validate_checkpoint_chain(
    root: Path,
    candidate: CandidateKey,
    checkpoint: StableArtifact,
    epoch: SelectionEpoch,
    *,
    batch_context: V2PreannotationBatchContext | None = None,
) -> tuple[list[str], ArtifactReference]:
    payload = checkpoint.payload
    if set(payload) != {
        "candidate",
        "completed_stages",
        "state",
        "outcome",
        "current_artifact_id",
    }:
        raise RuntimeError("pre-annotation checkpoint schema is invalid")
    if payload["candidate"] != candidate.to_value():
        raise RuntimeError("pre-annotation checkpoint candidate is invalid")
    completed = payload["completed_stages"]
    if type(completed) is not list or any(
        type(value) is not str for value in completed
    ):
        raise RuntimeError("pre-annotation checkpoint stages are invalid")
    if completed != list(PREANNOTATION_STAGES[: len(completed)]):
        raise RuntimeError("pre-annotation checkpoint is not a stage prefix")
    if not completed:
        raise RuntimeError("pre-annotation checkpoint has no completed stage")
    if payload["state"] not in {"active", "terminal", "eligible"}:
        raise RuntimeError("pre-annotation checkpoint state is invalid")
    if payload["state"] == "terminal":
        if payload["outcome"] not in _TERMINAL_OUTCOMES:
            raise RuntimeError("pre-annotation terminal outcome is invalid")
    elif payload["state"] == "eligible":
        if payload["outcome"] != "qualified" or completed != list(PREANNOTATION_STAGES):
            raise RuntimeError("pre-annotation eligible checkpoint is invalid")
    elif payload["outcome"] is not None:
        raise RuntimeError("active pre-annotation checkpoint has an outcome")

    previous = _load_patch(
        root,
        candidate,
        epoch,
        batch_context=batch_context,
    ).reference
    stage_artifacts: list[StableArtifact] = []
    for stage in completed:
        artifact = load_stable_artifact(
            root,
            artifact_id=(
                f"candidate_stage/{candidate.repository_key}/"
                f"{candidate.change_number}/{stage}/{epoch.label}"
            ),
            artifact_kind="candidate_stage",
            semantic_version=epoch.semantic_version,
        )
        _validate_stage(
            root,
            artifact,
            candidate,
            stage,
            previous,
            epoch,
            batch_context=batch_context,
        )
        previous = artifact.reference
        stage_artifacts.append(artifact)
    if payload["current_artifact_id"] != previous.artifact_id:
        raise RuntimeError("pre-annotation checkpoint current stage is dangling")
    if checkpoint.inputs != (previous,):
        raise RuntimeError("pre-annotation checkpoint input is dangling")
    if epoch.is_v2:
        _validate_v2_checkpoint_state(checkpoint, completed, stage_artifacts)
    return completed, previous


def _validate_v2_checkpoint_state(
    checkpoint: StableArtifact,
    completed: list[str],
    stages: list[StableArtifact],
) -> None:
    """Require the v2 checkpoint state machine to match its terminal stage."""

    states = [stage.payload["state"] for stage in stages]
    outcomes = [stage.payload["outcome"] for stage in stages]
    checkpoint_state = checkpoint.payload["state"]
    if checkpoint_state == "active":
        if states != ["passed"] * len(states):
            raise RuntimeError("v2 active checkpoint contains a final stage")
        return
    if checkpoint_state == "terminal":
        if states[:-1] != ["passed"] * (len(states) - 1):
            raise RuntimeError("v2 terminal checkpoint predecessor state is invalid")
        if states[-1] != "terminal" or outcomes[-1] != checkpoint.payload["outcome"]:
            raise RuntimeError("v2 terminal checkpoint final stage is invalid")
        return
    if checkpoint_state == "eligible":
        if completed != list(PREANNOTATION_STAGES):
            raise RuntimeError("v2 eligible checkpoint stage prefix is invalid")
        if states[:-1] != ["passed"] * (len(states) - 1):
            raise RuntimeError("v2 eligible checkpoint predecessor state is invalid")
        if states[-1] != "eligible" or outcomes[-1] != "qualified":
            raise RuntimeError("v2 eligible checkpoint final stage is invalid")
        return
    raise RuntimeError("v2 checkpoint state is invalid")


def _validate_stage(
    root: Path,
    artifact: StableArtifact,
    candidate: CandidateKey,
    stage: str,
    previous: ArtifactReference,
    epoch: SelectionEpoch,
    *,
    batch_context: V2PreannotationBatchContext | None = None,
) -> None:
    payload = artifact.payload
    if set(payload) != {
        "candidate",
        "stage",
        "state",
        "outcome",
        "details",
        "complete",
    }:
        raise RuntimeError("pre-annotation stage schema is invalid")
    details = payload.get("details")
    if type(details) is not dict:
        raise RuntimeError("pre-annotation stage details are invalid")
    evidence = _evidence_references(root, details, epoch)
    expected_inputs = (previous, *evidence)
    if stage == "test_manifest" and epoch.is_v2:
        atomicity = _load_accepted_source_atomicity(
            root,
            candidate,
            batch_context=batch_context,
        )
        if atomicity in evidence:
            raise RuntimeError("v2 test manifest duplicated its source atomicity input")
        expected_inputs = (previous, atomicity, *evidence)
    if (
        payload["candidate"] != candidate.to_value()
        or payload["stage"] != stage
        or payload["complete"] is not True
        or artifact.inputs != expected_inputs
    ):
        raise RuntimeError("pre-annotation stage binding is invalid")
    if payload["state"] not in {"passed", "terminal", "eligible"}:
        raise RuntimeError("pre-annotation stage state is invalid")


def _load_accepted_source_atomicity(
    root: Path,
    candidate: CandidateKey,
    *,
    batch_context: V2PreannotationBatchContext | None,
) -> ArtifactReference:
    atomicity_context = (
        batch_context.source_atomicity_context if batch_context is not None else None
    )
    if atomicity_context is None:
        atomicity_context = load_source_atomicity_batch_context(
            root,
            repository_key=candidate.repository_key,
        )
    receipt = load_source_atomicity_artifact(
        batch_context=atomicity_context,
        candidate=candidate,
    )
    if receipt.outcome != "accepted" or receipt.primary_issue_number is None:
        raise RuntimeError("v2 source atomicity is not accepted")
    return receipt.artifact.reference


def _evidence_references(
    root: Path,
    details: Mapping[str, object],
    epoch: SelectionEpoch,
) -> tuple[ArtifactReference, ...]:
    raw = details.get("evidence_artifacts")
    if raw is None:
        return ()
    if type(raw) is not list:
        raise RuntimeError("pre-annotation evidence references are invalid")
    references: list[ArtifactReference] = []
    for value in raw:
        if type(value) is not dict or set(value) != {
            "artifact_id",
            "artifact_kind",
            "semantic_version",
        }:
            raise RuntimeError("pre-annotation evidence reference is invalid")
        reference = ArtifactReference(
            artifact_id=value["artifact_id"],
            artifact_kind=value["artifact_kind"],
            semantic_version=value["semantic_version"],
        )
        if epoch.is_v2 and reference.semantic_version != epoch.semantic_version:
            raise RuntimeError("v2 pre-annotation evidence is not v2")
        loaded = load_stable_artifact(
            root,
            artifact_id=reference.artifact_id,
            artifact_kind=reference.artifact_kind,
            semantic_version=reference.semantic_version,
        )
        if loaded.reference != reference:
            raise RuntimeError("pre-annotation evidence identity is invalid")
        references.append(reference)
    if len(references) != len(set(references)):
        raise RuntimeError("pre-annotation evidence references are duplicated")
    return tuple(references)


def _write_checkpoint(
    root: Path,
    *,
    candidate: CandidateKey,
    completed: list[str],
    state: str,
    outcome: str | None,
    current: ArtifactReference,
    created_at: str,
    authorization: AuthorizationRecord,
    persistence_slots: threading.BoundedSemaphore,
    reload_slots: threading.BoundedSemaphore,
    epoch: SelectionEpoch,
) -> None:
    artifact_id = (
        f"preannotation_checkpoint/{candidate.repository_key}/"
        f"{candidate.change_number}/current/{epoch.label}"
    )
    with persistence_slots:
        write_stable_artifact(
            root,
            artifact_id=artifact_id,
            artifact_kind="preannotation_checkpoint",
            semantic_version=epoch.semantic_version,
            created_at=created_at,
            inputs=(current,),
            payload={
                "candidate": candidate.to_value(),
                "completed_stages": list(completed),
                "state": state,
                "outcome": outcome,
                "current_artifact_id": current.artifact_id,
            },
            authorization=authorization,
        )
    with reload_slots:
        loaded = load_stable_artifact(
            root,
            artifact_id=artifact_id,
            artifact_kind="preannotation_checkpoint",
            semantic_version=epoch.semantic_version,
        )
    if loaded.inputs != (current,):
        raise RuntimeError("pre-annotation checkpoint reload is inconsistent")


def _validate_configuration(
    candidates: tuple[CandidateKey, ...],
    operations: Mapping[str, Callable[[CandidateKey], StageDecision]],
    stages: tuple[str, ...],
    test_workers: int,
    build_workers: int,
    replay_workers: int,
    persistence_workers: int,
    reload_workers: int,
    queue_limit: int,
) -> None:
    if type(candidates) is not tuple or any(
        type(candidate) is not CandidateKey for candidate in candidates
    ):
        raise ValueError("pre-annotation candidates are invalid")
    if candidates != tuple(sorted(candidates, key=_candidate_order_key)) or len(
        set(candidates)
    ) != len(candidates):
        raise ValueError("pre-annotation candidates are unordered or duplicated")
    if type(operations) is not dict or tuple(operations) != stages:
        raise ValueError("pre-annotation operations are incomplete")
    limits = (
        test_workers,
        build_workers,
        replay_workers,
        persistence_workers,
        reload_workers,
        queue_limit,
    )
    if any(type(value) is not int or value <= 0 for value in limits):
        raise ValueError("pre-annotation concurrency is invalid")
    if (
        test_workers > 3
        or build_workers > 2
        or replay_workers > 3
        or persistence_workers > 2
        or reload_workers > 2
        or queue_limit > 16
    ):
        raise ValueError("pre-annotation concurrency exceeds the protocol")


def _stage_prefix(through_stage: str | None) -> tuple[str, ...]:
    if through_stage is None:
        return PREANNOTATION_STAGES
    if type(through_stage) is not str or through_stage not in PREANNOTATION_STAGES:
        raise ValueError("pre-annotation through stage is invalid")
    return PREANNOTATION_STAGES[: PREANNOTATION_STAGES.index(through_stage) + 1]


def _candidate_order_key(candidate: CandidateKey) -> tuple[int, int]:
    try:
        repository_rank = _REPOSITORY_RANK[candidate.repository_key]
    except KeyError as error:
        raise ValueError(
            "pre-annotation candidate repository is unsupported"
        ) from error
    return repository_rank, candidate.change_number


def _require_batch_candidate(
    root: Path,
    candidate: CandidateKey,
    batch_context: V2PreannotationBatchContext,
) -> None:
    if type(batch_context) is not V2PreannotationBatchContext:
        raise TypeError("v2 pre-annotation batch context is not typed")
    if batch_context.artifact_root != root.resolve(strict=True):
        raise RuntimeError("v2 pre-annotation batch root is inconsistent")
    if batch_context.repository_key != candidate.repository_key:
        raise RuntimeError("v2 pre-annotation batch repository is inconsistent")
    if candidate.change_number not in batch_context.candidate_numbers:
        raise RuntimeError("v2 candidate is outside the batch enumeration")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = [
    "PREANNOTATION_STAGES",
    "PreannotationPipelineResult",
    "StageDecision",
    "V2PreannotationBatchContext",
    "run_preannotation_pipeline",
]
