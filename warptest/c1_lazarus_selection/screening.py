"""Single-repository bounded source-to-patch screening."""

from __future__ import annotations

import json
import stat
import threading
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Callable, Literal

from .epoch import SELECTION_EPOCH_V1, SelectionEpoch, require_selection_epoch
from .models import CandidateKey
from .resource_guard import disk_capacity, require_write_capacity
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_MAX_CANDIDATES_PER_BATCH = 10_000


@dataclass(frozen=True)
class SourceDecision:
    status: Literal["accepted", "rejected", "blocked"]
    raw_evidence_path: str | None
    reason: str | None

    @classmethod
    def accepted(cls, *, raw_evidence_path: str) -> SourceDecision:
        return cls("accepted", raw_evidence_path, None)

    @classmethod
    def rejected(cls, reason: str) -> SourceDecision:
        return cls("rejected", None, reason)

    @classmethod
    def blocked(cls, reason: str) -> SourceDecision:
        return cls("blocked", None, reason)

    def __post_init__(self) -> None:
        if self.status == "accepted":
            if type(self.raw_evidence_path) is not str or not self.raw_evidence_path:
                raise ValueError("accepted source decision lacks raw evidence")
            if self.reason is not None:
                raise ValueError("accepted source decision carries a rejection")
        elif self.status in {"rejected", "blocked"}:
            if type(self.reason) is not str or not self.reason:
                raise ValueError("rejected source decision lacks a reason")
            if self.raw_evidence_path is not None:
                raise ValueError("rejected source decision carries accepted evidence")
        else:
            raise ValueError("source decision status is invalid")


@dataclass(frozen=True)
class PatchDecision:
    status: Literal["eligible", "production_only", "rejected"]
    production_paths: tuple[str, ...]
    test_paths: tuple[str, ...]
    reason: str | None

    @classmethod
    def eligible(
        cls,
        *,
        production_paths: tuple[str, ...],
        test_paths: tuple[str, ...],
    ) -> PatchDecision:
        return cls("eligible", production_paths, test_paths, None)

    @classmethod
    def production_only(
        cls,
        *,
        production_paths: tuple[str, ...],
    ) -> PatchDecision:
        """Accept a repair that ships no test of its own.

        This is the P2P-only track's entry state.  It stays a distinct status so
        the strict track's ``eligible`` still means "carries its own relevant
        regression test", and so a reader cannot mistake one for the other.
        """

        return cls("production_only", production_paths, (), None)

    @classmethod
    def rejected(cls, reason: str) -> PatchDecision:
        return cls("rejected", (), (), reason)

    def __post_init__(self) -> None:
        if self.status == "eligible":
            if (
                not self.production_paths
                or not self.test_paths
                or self.reason is not None
            ):
                raise ValueError("eligible patch decision is incomplete")
        elif self.status == "production_only":
            if not self.production_paths or self.test_paths or self.reason is not None:
                raise ValueError("production-only patch decision is invalid")
        elif self.status == "rejected":
            if self.production_paths or self.test_paths or not self.reason:
                raise ValueError("rejected patch decision is invalid")
        else:
            raise ValueError("patch decision status is invalid")


@dataclass(frozen=True)
class ScreeningBatchResult:
    repository_key: str
    total_candidates: int
    scheduled_candidates: int
    resumed_candidates: int
    source_rejected: int
    patch_rejected: int
    patch_eligible: int
    infrastructure_blocked: int
    enumeration_loads: int
    max_in_flight: int
    paused_for_disk: bool
    remaining_candidates: int


@dataclass(frozen=True)
class _PersistedSource:
    candidate: CandidateKey
    decision: SourceDecision
    reference: ArtifactReference


@dataclass(frozen=True)
class RawPatchEvidence:
    """A patch file declared by the durable source-capture manifest."""

    source_manifest_path: str
    raw_patch_path: str
    patch_path: Path


@dataclass(frozen=True)
class _PersistedTerminal:
    candidate: CandidateKey
    outcome: Literal[
        "source_rejected",
        "patch_rejected",
        "patch_eligible",
        "infrastructure_blocked",
    ]


class _ReloadMeter:
    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._current = 0
        self._lock = threading.Lock()

    def __enter__(self) -> None:
        with self._lock:
            self._current += 1
            if self._current > self._limit:
                raise RuntimeError("typed reload concurrency exceeded its limit")

    def __exit__(self, *_args: object) -> None:
        with self._lock:
            self._current -= 1


def run_screening_batch(
    artifact_root: str | Path,
    *,
    repository_key: str,
    source_capture: Callable[[CandidateKey], SourceDecision],
    patch_capture: Callable[[CandidateKey, SourceDecision], PatchDecision],
    authorization: AuthorizationRecord,
    selection_epoch: SelectionEpoch = SELECTION_EPOCH_V1,
    source_workers: int = 8,
    patch_workers: int = 4,
    persistence_workers: int = 2,
    reload_workers: int = 2,
    queue_limit: int = 16,
    max_candidates: int | None = None,
) -> ScreeningBatchResult:
    """Run one ordered repository batch and resume structurally valid heads.

    When ``max_candidates`` is set, this invocation schedules only the first
    unresolved candidates in the enumeration's fixed order.  Completed heads
    are still typed-loaded across the whole population, so a later invocation
    resumes them and continues with the next unresolved prefix.
    """

    epoch = require_selection_epoch(selection_epoch)
    _validate_limits(
        source_workers,
        patch_workers,
        persistence_workers,
        reload_workers,
        queue_limit,
    )
    _validate_max_candidates(max_candidates)
    root = Path(artifact_root)
    enumeration = load_stable_artifact(
        root,
        artifact_id=(f"repository_enumeration/{repository_key}/current/{epoch.label}"),
        artifact_kind="repository_enumeration",
        semantic_version=epoch.semantic_version,
    )
    enumeration_reference = epoch.enumeration_reference(repository_key)
    candidates = _enumeration_candidates(enumeration, repository_key)
    outcomes: dict[int, str] = {}
    unresolved: list[CandidateKey] = []
    for candidate in candidates:
        checkpoint = _load_checkpoint_if_present(root, candidate, epoch)
        if checkpoint is None:
            unresolved.append(candidate)
            continue
        state = checkpoint.payload.get("state")
        if state == "complete":
            outcome = checkpoint.payload.get("outcome")
            if outcome not in {
                "source_rejected",
                "patch_rejected",
                "patch_eligible",
                "infrastructure_blocked",
            }:
                raise RuntimeError("screening checkpoint outcome is invalid")
            if epoch.is_v2:
                _validate_v2_checkpoint_resume(
                    root,
                    candidate,
                    checkpoint,
                    enumeration_reference,
                    outcome,
                    epoch,
                )
            outcomes[candidate.change_number] = outcome
        elif state == "redo_required":
            unresolved.append(candidate)
        else:
            raise RuntimeError("screening checkpoint state is invalid")

    if max_candidates is None:
        pending: deque[CandidateKey] = deque(unresolved)
        deferred_candidates = 0
    else:
        pending = deque(unresolved[:max_candidates])
        deferred_candidates = len(unresolved) - len(pending)

    submitted = 0
    max_in_flight = 0
    paused_for_disk = False
    ready_for_patch: deque[_PersistedSource] = deque()
    source_futures: dict[Future[SourceDecision], CandidateKey] = {}
    patch_futures: dict[Future[PatchDecision], _PersistedSource] = {}
    persist_source_futures: dict[
        Future[_PersistedSource | _PersistedTerminal], CandidateKey
    ] = {}
    persist_patch_futures: dict[Future[_PersistedTerminal], CandidateKey] = {}
    reload_meter = _ReloadMeter(reload_workers)

    def active() -> int:
        return (
            len(source_futures)
            + len(persist_source_futures)
            + len(ready_for_patch)
            + len(patch_futures)
            + len(persist_patch_futures)
        )

    with (
        ThreadPoolExecutor(max_workers=source_workers) as source_pool,
        ThreadPoolExecutor(max_workers=patch_workers) as patch_pool,
        ThreadPoolExecutor(max_workers=persistence_workers) as persistence_pool,
    ):
        while pending or active():
            capacity = disk_capacity(root)
            if pending and not capacity.may_start_candidate:
                paused_for_disk = True
            while (
                pending
                and capacity.may_start_candidate
                and len(source_futures) < source_workers
                and active() < queue_limit
            ):
                candidate = pending.popleft()
                source_futures[source_pool.submit(source_capture, candidate)] = (
                    candidate
                )
                submitted += 1
            while ready_for_patch and len(patch_futures) < patch_workers:
                source = ready_for_patch.popleft()
                patch_futures[
                    patch_pool.submit(patch_capture, source.candidate, source.decision)
                ] = source
            max_in_flight = max(max_in_flight, active())
            futures = tuple(
                [
                    *source_futures,
                    *persist_source_futures,
                    *patch_futures,
                    *persist_patch_futures,
                ]
            )
            if not futures:
                break
            completed, _ = wait(futures, return_when=FIRST_COMPLETED)
            for future in completed:
                if future in source_futures:
                    candidate = source_futures.pop(future)
                    decision = future.result()
                    if type(decision) is not SourceDecision:
                        raise RuntimeError(
                            "source capture returned an untyped decision"
                        )
                    persisted = persistence_pool.submit(
                        _persist_source_decision,
                        root,
                        candidate,
                        decision,
                        authorization,
                        reload_meter,
                        enumeration_reference,
                        epoch,
                    )
                    persist_source_futures[persisted] = candidate
                elif future in persist_source_futures:
                    candidate = persist_source_futures.pop(future)
                    persisted_source = future.result()
                    if isinstance(persisted_source, _PersistedTerminal):
                        outcomes[candidate.change_number] = persisted_source.outcome
                    else:
                        ready_for_patch.append(persisted_source)
                elif future in patch_futures:
                    source = patch_futures.pop(future)
                    decision = future.result()
                    if type(decision) is not PatchDecision:
                        raise RuntimeError("patch capture returned an untyped decision")
                    persisted = persistence_pool.submit(
                        _persist_patch_decision,
                        root,
                        source,
                        decision,
                        authorization,
                        reload_meter,
                        enumeration_reference,
                        epoch,
                    )
                    persist_patch_futures[persisted] = source.candidate
                else:
                    candidate = persist_patch_futures.pop(future)
                    terminal = future.result()
                    outcomes[candidate.change_number] = terminal.outcome

    # At this point every submitted candidate has a typed terminal.  What is
    # left is precisely the intentionally deferred prefix tail plus any work
    # held back by the disk guard; completed checkpoints are never counted.
    remaining = deferred_candidates + len(pending)
    return ScreeningBatchResult(
        repository_key=repository_key,
        total_candidates=len(candidates),
        scheduled_candidates=submitted,
        resumed_candidates=len(outcomes) - submitted,
        source_rejected=sum(value == "source_rejected" for value in outcomes.values()),
        patch_rejected=sum(value == "patch_rejected" for value in outcomes.values()),
        patch_eligible=sum(value == "patch_eligible" for value in outcomes.values()),
        infrastructure_blocked=sum(
            value == "infrastructure_blocked" for value in outcomes.values()
        ),
        enumeration_loads=1,
        max_in_flight=max_in_flight,
        paused_for_disk=paused_for_disk,
        remaining_candidates=remaining,
    )


def _persist_source_decision(
    root: Path,
    candidate: CandidateKey,
    decision: SourceDecision,
    authorization: AuthorizationRecord,
    reload_meter: _ReloadMeter,
    enumeration_reference: ArtifactReference,
    epoch: SelectionEpoch,
) -> _PersistedSource | _PersistedTerminal:
    require_write_capacity(root)
    created_at = _now()
    if decision.status in {"rejected", "blocked"}:
        kind = "candidate_source_terminal"
        artifact_id = (
            f"candidate_source_terminal/{candidate.repository_key}/"
            f"{candidate.change_number}/source/{epoch.label}"
        )
        outcome = (
            "source_rejected"
            if decision.status == "rejected"
            else "infrastructure_blocked"
        )
        payload = {
            "candidate": candidate.to_value(),
            "outcome": outcome,
            "reason": decision.reason,
            "complete": True,
        }
        _write_and_reload(
            root,
            artifact_id=artifact_id,
            artifact_kind=kind,
            created_at=created_at,
            inputs=(enumeration_reference,),
            payload=payload,
            authorization=authorization,
            reload_meter=reload_meter,
            epoch=epoch,
        )
        _write_checkpoint(
            root,
            candidate,
            outcome=outcome,
            stage=ArtifactReference(artifact_id, kind, epoch.semantic_version),
            created_at=created_at,
            authorization=authorization,
            reload_meter=reload_meter,
            enumeration_reference=enumeration_reference,
            epoch=epoch,
        )
        return _PersistedTerminal(candidate, outcome)
    kind = "candidate_source_packet"
    artifact_id = (
        f"candidate_source_packet/{candidate.repository_key}/"
        f"{candidate.change_number}/source/{epoch.label}"
    )
    _write_and_reload(
        root,
        artifact_id=artifact_id,
        artifact_kind=kind,
        created_at=created_at,
        inputs=(enumeration_reference,),
        payload={
            "candidate": candidate.to_value(),
            "raw_evidence_path": decision.raw_evidence_path,
            "complete": True,
        },
        authorization=authorization,
        reload_meter=reload_meter,
        epoch=epoch,
    )
    return _PersistedSource(
        candidate,
        decision,
        ArtifactReference(artifact_id, kind, epoch.semantic_version),
    )


def _persist_patch_decision(
    root: Path,
    source: _PersistedSource,
    decision: PatchDecision,
    authorization: AuthorizationRecord,
    reload_meter: _ReloadMeter,
    enumeration_reference: ArtifactReference,
    epoch: SelectionEpoch,
) -> _PersistedTerminal:
    require_write_capacity(root)
    candidate = source.candidate
    created_at = _now()
    if decision.status == "eligible":
        raw_patch_evidence: RawPatchEvidence | None = None
        if epoch.is_v2:
            raw_patch_evidence = _resolve_v2_source_patch_evidence(
                root,
                source,
                enumeration_reference,
            )
        kind = "candidate_test_patch"
        artifact_id = (
            f"candidate_test_patch/{candidate.repository_key}/"
            f"{candidate.change_number}/screening/{epoch.label}"
        )
        outcome: Literal["patch_eligible", "patch_rejected"] = "patch_eligible"
        payload = {
            "candidate": candidate.to_value(),
            "outcome": outcome,
            "production_paths": list(decision.production_paths),
            "test_paths": list(decision.test_paths),
            "complete": True,
        }
        if raw_patch_evidence is not None:
            payload["source_manifest_path"] = raw_patch_evidence.source_manifest_path
            payload["raw_patch_path"] = raw_patch_evidence.raw_patch_path
    else:
        kind = "candidate_patch_terminal"
        artifact_id = (
            f"candidate_patch_terminal/{candidate.repository_key}/"
            f"{candidate.change_number}/patch/{epoch.label}"
        )
        outcome = "patch_rejected"
        payload = {
            "candidate": candidate.to_value(),
            "outcome": outcome,
            "reason": decision.reason,
            "complete": True,
        }
    _write_and_reload(
        root,
        artifact_id=artifact_id,
        artifact_kind=kind,
        created_at=created_at,
        inputs=(enumeration_reference, source.reference),
        payload=payload,
        authorization=authorization,
        reload_meter=reload_meter,
        epoch=epoch,
    )
    _write_checkpoint(
        root,
        candidate,
        outcome=outcome,
        stage=ArtifactReference(artifact_id, kind, epoch.semantic_version),
        created_at=created_at,
        authorization=authorization,
        reload_meter=reload_meter,
        enumeration_reference=enumeration_reference,
        epoch=epoch,
    )
    return _PersistedTerminal(candidate, outcome)


def _resolve_v2_source_patch_evidence(
    root: Path,
    source: _PersistedSource,
    enumeration_reference: ArtifactReference,
) -> RawPatchEvidence:
    source_packet = _load_referenced_artifact(root, source.reference)
    raw_evidence_path = source_packet.payload.get("raw_evidence_path")
    if (
        source_packet.inputs != (enumeration_reference,)
        or source_packet.payload.get("candidate") != source.candidate.to_value()
        or source_packet.payload.get("complete") is not True
        or type(raw_evidence_path) is not str
        or not raw_evidence_path
        or raw_evidence_path != source.decision.raw_evidence_path
    ):
        raise RuntimeError("v2 source packet raw evidence binding is invalid")
    return resolve_raw_patch_evidence(
        root,
        candidate=source.candidate,
        source_manifest_path=raw_evidence_path,
    )


def _write_checkpoint(
    root: Path,
    candidate: CandidateKey,
    *,
    outcome: str,
    stage: ArtifactReference,
    created_at: str,
    authorization: AuthorizationRecord,
    reload_meter: _ReloadMeter,
    enumeration_reference: ArtifactReference,
    epoch: SelectionEpoch,
) -> None:
    checkpoint_id = (
        f"screening_checkpoint/{candidate.repository_key}/"
        f"{candidate.change_number}/current/{epoch.label}"
    )
    _write_and_reload(
        root,
        artifact_id=checkpoint_id,
        artifact_kind="screening_checkpoint",
        created_at=created_at,
        inputs=(enumeration_reference, stage),
        payload={
            "candidate": candidate.to_value(),
            "state": "complete",
            "outcome": outcome,
            "stage_artifact_id": stage.artifact_id,
        },
        authorization=authorization,
        reload_meter=reload_meter,
        epoch=epoch,
    )


def _write_and_reload(
    root: Path,
    *,
    artifact_id: str,
    artifact_kind: str,
    created_at: str,
    inputs: tuple[ArtifactReference, ...],
    payload: dict[str, object],
    authorization: AuthorizationRecord,
    reload_meter: _ReloadMeter,
    epoch: SelectionEpoch = SELECTION_EPOCH_V1,
) -> StableArtifact:
    write_stable_artifact(
        root,
        artifact_id=artifact_id,
        artifact_kind=artifact_kind,
        semantic_version=epoch.semantic_version,
        created_at=created_at,
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    with reload_meter:
        return load_stable_artifact(
            root,
            artifact_id=artifact_id,
            artifact_kind=artifact_kind,
            semantic_version=epoch.semantic_version,
        )


def _load_checkpoint_if_present(
    root: Path,
    candidate: CandidateKey,
    epoch: SelectionEpoch,
) -> StableArtifact | None:
    try:
        return load_stable_artifact(
            root,
            artifact_id=(
                f"screening_checkpoint/{candidate.repository_key}/"
                f"{candidate.change_number}/current/{epoch.label}"
            ),
            artifact_kind="screening_checkpoint",
            semantic_version=epoch.semantic_version,
        )
    except RuntimeError as error:
        if str(error) in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            return None
        raise


def _validate_v2_checkpoint_resume(
    root: Path,
    candidate: CandidateKey,
    checkpoint: StableArtifact,
    enumeration_reference: ArtifactReference,
    outcome: str,
    epoch: SelectionEpoch,
) -> None:
    """Require the complete v2 source/patch dependency chain before resume."""

    if checkpoint.payload.get("candidate") != candidate.to_value():
        raise RuntimeError("screening checkpoint candidate is invalid")
    expected_stage = _stage_reference(candidate, outcome, epoch)
    if len(checkpoint.inputs) != 2 or checkpoint.inputs[0] != enumeration_reference:
        raise RuntimeError("screening checkpoint enumeration input is invalid")
    if checkpoint.inputs[1] != expected_stage:
        raise RuntimeError("screening checkpoint stage reference is invalid")
    if checkpoint.payload.get("stage_artifact_id") != expected_stage.artifact_id:
        raise RuntimeError("screening checkpoint stage artifact is invalid")

    stage = _load_referenced_artifact(root, expected_stage)
    if stage.payload.get("candidate") != candidate.to_value():
        raise RuntimeError("screening stage candidate is invalid")
    if stage.payload.get("complete") is not True:
        raise RuntimeError("screening stage is incomplete")
    if stage.payload.get("outcome") != outcome:
        raise RuntimeError("screening stage outcome is invalid")
    if outcome in {"source_rejected", "infrastructure_blocked"}:
        if stage.inputs != (enumeration_reference,):
            raise RuntimeError("screening source terminal enumeration input is invalid")
        return

    source_reference = _source_packet_reference(candidate, epoch)
    if stage.inputs != (enumeration_reference, source_reference):
        raise RuntimeError("screening patch source prerequisite is invalid")
    source = _load_referenced_artifact(root, source_reference)
    if source.inputs != (enumeration_reference,):
        raise RuntimeError("screening source packet enumeration input is invalid")
    if source.payload.get("candidate") != candidate.to_value():
        raise RuntimeError("screening source packet candidate is invalid")
    if source.payload.get("complete") is not True:
        raise RuntimeError("screening source packet is incomplete")
    raw_evidence_path = source.payload.get("raw_evidence_path")
    if type(raw_evidence_path) is not str or not raw_evidence_path:
        raise RuntimeError("screening source packet evidence is invalid")
    if outcome == "patch_eligible":
        raw_patch_evidence = resolve_raw_patch_evidence(
            root,
            candidate=candidate,
            source_manifest_path=raw_evidence_path,
        )
        if (
            stage.payload.get("source_manifest_path")
            != raw_patch_evidence.source_manifest_path
            or stage.payload.get("raw_patch_path") != raw_patch_evidence.raw_patch_path
        ):
            raise RuntimeError("screening patch raw evidence binding is invalid")


def _stage_reference(
    candidate: CandidateKey,
    outcome: str,
    epoch: SelectionEpoch,
) -> ArtifactReference:
    if outcome in {"source_rejected", "infrastructure_blocked"}:
        return ArtifactReference(
            artifact_id=(
                f"candidate_source_terminal/{candidate.repository_key}/"
                f"{candidate.change_number}/source/{epoch.label}"
            ),
            artifact_kind="candidate_source_terminal",
            semantic_version=epoch.semantic_version,
        )
    if outcome == "patch_rejected":
        return ArtifactReference(
            artifact_id=(
                f"candidate_patch_terminal/{candidate.repository_key}/"
                f"{candidate.change_number}/patch/{epoch.label}"
            ),
            artifact_kind="candidate_patch_terminal",
            semantic_version=epoch.semantic_version,
        )
    if outcome == "patch_eligible":
        return ArtifactReference(
            artifact_id=(
                f"candidate_test_patch/{candidate.repository_key}/"
                f"{candidate.change_number}/screening/{epoch.label}"
            ),
            artifact_kind="candidate_test_patch",
            semantic_version=epoch.semantic_version,
        )
    raise RuntimeError("screening checkpoint outcome is invalid")


def _source_packet_reference(
    candidate: CandidateKey,
    epoch: SelectionEpoch,
) -> ArtifactReference:
    return ArtifactReference(
        artifact_id=(
            f"candidate_source_packet/{candidate.repository_key}/"
            f"{candidate.change_number}/source/{epoch.label}"
        ),
        artifact_kind="candidate_source_packet",
        semantic_version=epoch.semantic_version,
    )


def resolve_raw_patch_evidence(
    artifact_root: str | Path,
    *,
    candidate: CandidateKey,
    source_manifest_path: object,
) -> RawPatchEvidence:
    """Resolve the patch named by one complete, durable source manifest.

    The source provider persists the raw manifest before parsing it.  A
    screening artifact may therefore bind only a path that the manifest itself
    declares; accepting an arbitrary patch path from a later callback would
    sever the source-to-patch evidence chain.
    """

    root = Path(artifact_root).resolve(strict=True)
    manifest_path, canonical_manifest_path = _safe_raw_file(
        root,
        source_manifest_path,
        label="source manifest",
    )
    manifest = _strict_raw_json_object(manifest_path, "source raw manifest")
    if (
        manifest.get("candidate") != candidate.to_value()
        or manifest.get("complete") is not True
    ):
        raise RuntimeError("source raw manifest candidate binding is invalid")
    raw_paths = manifest.get("raw_paths")
    patch_raw_path = manifest.get("patch_raw_path")
    if (
        type(raw_paths) is not list
        or not raw_paths
        or any(type(path) is not str or not path for path in raw_paths)
        or type(patch_raw_path) is not str
        or not patch_raw_path
        or patch_raw_path not in raw_paths
    ):
        raise RuntimeError("source raw manifest patch inventory is invalid")
    patch_path, canonical_patch_path = _safe_raw_file(
        root,
        patch_raw_path,
        label="source patch",
    )
    if not patch_path.read_bytes():
        raise RuntimeError("source patch is empty")
    return RawPatchEvidence(
        source_manifest_path=canonical_manifest_path,
        raw_patch_path=canonical_patch_path,
        patch_path=patch_path,
    )


def _safe_raw_file(root: Path, value: object, *, label: str) -> tuple[Path, str]:
    if type(value) is not str or not value:
        raise RuntimeError(f"{label} path is missing")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or not relative.parts
        or "." in relative.parts
        or ".." in relative.parts
        or value != relative.as_posix()
    ):
        raise RuntimeError(f"{label} path is unsafe")
    path = root.joinpath(*relative.parts)
    current = root
    for part in relative.parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError as error:
            raise RuntimeError(f"{label} file is missing") from error
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError(f"{label} path contains a symlink")
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError(f"{label} file is unsafe")
    return path, relative.as_posix()


def _strict_raw_json_object(path: Path, label: str) -> dict[str, object]:
    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in values:
            if key in result:
                raise RuntimeError(f"{label} contains a duplicate key")
            result[key] = value
        return result

    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                RuntimeError(f"{label} contains a non-finite number")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"{label} is not strict JSON") from error
    if type(value) is not dict:
        raise RuntimeError(f"{label} is not an object")
    return value


def _load_referenced_artifact(
    root: Path,
    reference: ArtifactReference,
) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _enumeration_candidates(
    artifact: StableArtifact,
    repository_key: str,
) -> tuple[CandidateKey, ...]:
    payload = artifact.payload
    if (
        payload.get("repository_key") != repository_key
        or payload.get("complete") is not True
    ):
        raise RuntimeError("repository enumeration is incomplete")
    raw = payload.get("candidates")
    if type(raw) is not list:
        raise RuntimeError("repository enumeration candidates are invalid")
    candidates = tuple(CandidateKey.from_value(value) for value in raw)
    if candidates != tuple(sorted(candidates)) or len(set(candidates)) != len(
        candidates
    ):
        raise RuntimeError(
            "repository enumeration candidates are unordered or duplicated"
        )
    return candidates


def _validate_limits(
    source_workers: int,
    patch_workers: int,
    persistence_workers: int,
    reload_workers: int,
    queue_limit: int,
) -> None:
    values = (
        source_workers,
        patch_workers,
        persistence_workers,
        reload_workers,
        queue_limit,
    )
    if any(type(value) is not int or value <= 0 for value in values):
        raise ValueError("screening concurrency limits are invalid")
    if (
        source_workers > 12
        or patch_workers > 4
        or persistence_workers > 2
        or reload_workers > 2
        or queue_limit > 16
    ):
        raise ValueError("screening concurrency exceeds the protocol")


def _validate_max_candidates(max_candidates: int | None) -> None:
    if max_candidates is None:
        return
    if (
        type(max_candidates) is not int
        or max_candidates < 1
        or max_candidates > _MAX_CANDIDATES_PER_BATCH
    ):
        raise ValueError(
            "screening max_candidates must be an integer between 1 and 10000"
        )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = [
    "PatchDecision",
    "RawPatchEvidence",
    "ScreeningBatchResult",
    "SourceDecision",
    "resolve_raw_patch_evidence",
    "run_screening_batch",
]
