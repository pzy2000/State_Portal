"""Stable formal schedule and explicitly authorized pair execution."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Literal

from .models import CandidateKey
from .resource_guard import require_write_capacity
from .readiness import load_implementation_readiness
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_ARMS = ("state", "agent-s")
_STATES = ("buggy", "fixed")
_SEEDS = (0, 1, 2)


@dataclass(frozen=True)
class FormalTask:
    task_id: str
    candidate: CandidateKey
    locus: Literal["at_goal", "on_path"]

    def __post_init__(self) -> None:
        if type(self.task_id) is not str or not self.task_id:
            raise ValueError("formal task ID is invalid")
        if type(self.candidate) is not CandidateKey:
            raise ValueError("formal task candidate is invalid")
        if self.locus not in {"at_goal", "on_path"}:
            raise ValueError("formal task locus is invalid")


@dataclass(frozen=True)
class TrialCapture:
    state: Literal["buggy", "fixed"]
    raw_evidence_path: str
    checkpoint_path: str | None

    def __post_init__(self) -> None:
        if self.state not in _STATES:
            raise ValueError("trial capture state is invalid")
        if type(self.raw_evidence_path) is not str or not self.raw_evidence_path:
            raise ValueError("trial raw evidence path is invalid")
        if self.state == "buggy":
            if type(self.checkpoint_path) is not str or not self.checkpoint_path:
                raise ValueError("buggy trial capture lacks checkpoint")
        elif self.checkpoint_path is not None:
            raise ValueError("fixed trial capture carries a checkpoint")


@dataclass(frozen=True)
class FormalRunResult:
    total_pairs: int
    completed_pairs: int
    resumed_pairs: int
    completed_cells: int


def write_formal_schedule(
    artifact_root: str | Path,
    *,
    tasks: tuple[FormalTask, ...],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Write the deterministic 12-cell-per-task formal schedule."""

    if type(tasks) is not tuple or any(type(task) is not FormalTask for task in tasks):
        raise ValueError("formal tasks are invalid")
    if len({task.task_id for task in tasks}) != len(tasks):
        raise ValueError("formal task IDs are duplicated")
    if len({task.candidate for task in tasks}) != len(tasks):
        raise ValueError("formal task candidates are duplicated")
    mode = "confirmatory" if len(tasks) == 24 else "exploratory"
    cells = [
        {
            "task_id": task.task_id,
            "seed": seed,
            "arm": arm,
            "state": state,
        }
        for task in tasks
        for seed in _SEEDS
        for arm in _ARMS
        for state in _STATES
    ]
    payload = {
        "tasks": [
            {
                "task_id": task.task_id,
                "candidate": task.candidate.to_value(),
                "locus": task.locus,
            }
            for task in tasks
        ],
        "seeds": list(_SEEDS),
        "arms": list(_ARMS),
        "state_order": list(_STATES),
        "cells": cells,
        "cell_count": len(cells),
        "analysis_mode": mode,
        "execution_authorized": False,
    }
    write_stable_artifact(
        artifact_root,
        artifact_id="formal_schedule/current/v1",
        artifact_kind="formal_schedule",
        semantic_version="1.0",
        created_at=_now(),
        inputs=(),
        payload=payload,
        authorization=authorization,
    )
    loaded = load_stable_artifact(
        artifact_root,
        artifact_id="formal_schedule/current/v1",
        artifact_kind="formal_schedule",
        semantic_version="1.0",
    )
    if loaded.payload != payload:
        raise RuntimeError("formal schedule reload is inconsistent")
    return loaded


def write_execution_authorization(
    artifact_root: str | Path,
    *,
    schedule_artifact_id: str,
    readiness_artifact_id: str,
    action: Literal["confirmatory_trials", "exploratory_trials"],
    confirmation_reference: str,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Record one explicit user confirmation for one exact schedule head."""

    schedule = _load_schedule(artifact_root, schedule_artifact_id)
    readiness = load_implementation_readiness(
        artifact_root,
        scope=schedule,
        artifact_id=readiness_artifact_id,
    )
    expected_action = (
        "confirmatory_trials"
        if schedule.payload["analysis_mode"] == "confirmatory"
        else "exploratory_trials"
    )
    if action != expected_action:
        raise ValueError("execution authorization action does not match schedule")
    if type(confirmation_reference) is not str or not confirmation_reference:
        raise ValueError("execution confirmation reference is invalid")
    artifact_id = f"execution_authorization/{action}/current/v1"
    write_stable_artifact(
        artifact_root,
        artifact_id=artifact_id,
        artifact_kind="execution_authorization",
        semantic_version="1.0",
        created_at=_now(),
        inputs=(schedule.reference, readiness.reference),
        payload={
            "schedule_artifact_id": schedule.reference.artifact_id,
            "schedule_created_at": schedule.created_at,
            "readiness_artifact_id": readiness.reference.artifact_id,
            "action": action,
            "confirmation_reference": confirmation_reference,
            "active": True,
        },
        authorization=authorization,
    )
    return load_stable_artifact(
        artifact_root,
        artifact_id=artifact_id,
        artifact_kind="execution_authorization",
        semantic_version="1.0",
    )


def run_formal_trials(
    artifact_root: str | Path,
    *,
    schedule_artifact_id: str,
    execute: Callable[[FormalTask, int, str, str, str | None], TrialCapture],
    authorization: AuthorizationRecord,
    outer_workers: int = 8,
) -> FormalRunResult:
    """Run authorized task/seed units; each arm remains buggy then fixed."""

    if type(outer_workers) is not int or not 1 <= outer_workers <= 8:
        raise ValueError("formal outer-worker count is invalid")
    schedule = _load_schedule(artifact_root, schedule_artifact_id)
    action = (
        "confirmatory_trials"
        if schedule.payload["analysis_mode"] == "confirmatory"
        else "exploratory_trials"
    )
    grant = _load_execution_authorization(artifact_root, schedule, action)
    tasks = _schedule_tasks(schedule)
    units = tuple((task, seed) for task in tasks for seed in _SEEDS)

    def run(unit: tuple[FormalTask, int]) -> tuple[bool, int]:
        task, seed = unit
        existing = _load_pair_if_present(artifact_root, task, seed)
        if existing is not None:
            _validate_pair(artifact_root, existing, task, seed, schedule, grant)
            return True, 4
        references: list[ArtifactReference] = []
        for arm in _ARMS:
            checkpoint: str | None = None
            previous: ArtifactReference | None = None
            for state in _STATES:
                capture = execute(task, seed, arm, state, checkpoint)
                if type(capture) is not TrialCapture or capture.state != state:
                    raise RuntimeError("formal trial returned an invalid capture")
                inputs = [schedule.reference, grant.reference]
                if previous is not None:
                    inputs.append(previous)
                cell_id = f"formal_trial/{task.task_id}/{seed}/{arm}/{state}/v1"
                created_at = _now()
                require_write_capacity(artifact_root)
                write_stable_artifact(
                    artifact_root,
                    artifact_id=cell_id,
                    artifact_kind="formal_trial",
                    semantic_version="1.0",
                    created_at=created_at,
                    inputs=tuple(inputs),
                    payload={
                        "task_id": task.task_id,
                        "candidate": task.candidate.to_value(),
                        "locus": task.locus,
                        "seed": seed,
                        "arm": arm,
                        "state": state,
                        "raw_evidence_path": capture.raw_evidence_path,
                        "checkpoint_path": capture.checkpoint_path,
                        "complete": True,
                    },
                    authorization=authorization,
                )
                cell = load_stable_artifact(
                    artifact_root,
                    artifact_id=cell_id,
                    artifact_kind="formal_trial",
                    semantic_version="1.0",
                )
                references.append(cell.reference)
                previous = cell.reference
                checkpoint = capture.checkpoint_path
        pair_id = f"formal_pair_checkpoint/{task.task_id}/{seed}/current/v1"
        write_stable_artifact(
            artifact_root,
            artifact_id=pair_id,
            artifact_kind="formal_pair_checkpoint",
            semantic_version="1.0",
            created_at=_now(),
            inputs=tuple(references),
            payload={
                "task_id": task.task_id,
                "seed": seed,
                "cell_artifact_ids": [value.artifact_id for value in references],
                "state": "complete",
            },
            authorization=authorization,
        )
        pair = load_stable_artifact(
            artifact_root,
            artifact_id=pair_id,
            artifact_kind="formal_pair_checkpoint",
            semantic_version="1.0",
        )
        _validate_pair(artifact_root, pair, task, seed, schedule, grant)
        return False, 4

    with ThreadPoolExecutor(max_workers=outer_workers) as pool:
        results = tuple(pool.map(run, units))
    return FormalRunResult(
        total_pairs=len(units),
        completed_pairs=len(units),
        resumed_pairs=sum(resumed for resumed, _cells in results),
        completed_cells=sum(cells for _resumed, cells in results),
    )


def _load_schedule(root: str | Path, artifact_id: str) -> StableArtifact:
    schedule = load_stable_artifact(
        root,
        artifact_id=artifact_id,
        artifact_kind="formal_schedule",
        semantic_version="1.0",
    )
    if schedule.payload.get(
        "execution_authorized"
    ) is not False or schedule.payload.get("analysis_mode") not in {
        "confirmatory",
        "exploratory",
    }:
        raise RuntimeError("formal schedule is invalid")
    return schedule


def _load_execution_authorization(
    root: str | Path,
    schedule: StableArtifact,
    action: str,
) -> StableArtifact:
    try:
        grant = load_stable_artifact(
            root,
            artifact_id=f"execution_authorization/{action}/current/v1",
            artifact_kind="execution_authorization",
            semantic_version="1.0",
        )
    except RuntimeError as error:
        raise RuntimeError("formal execution authorization is missing") from error
    payload = grant.payload
    if (
        len(grant.inputs) != 2
        or grant.inputs[0] != schedule.reference
        or payload.get("schedule_artifact_id") != schedule.reference.artifact_id
        or payload.get("schedule_created_at") != schedule.created_at
        or payload.get("action") != action
        or payload.get("active") is not True
        or type(payload.get("confirmation_reference")) is not str
    ):
        raise RuntimeError("formal execution authorization is invalid")
    readiness = load_implementation_readiness(
        root,
        scope=schedule,
        artifact_id=payload.get("readiness_artifact_id"),
    )
    if grant.inputs[1] != readiness.reference:
        raise RuntimeError("formal execution readiness is invalid")
    return grant


def _schedule_tasks(schedule: StableArtifact) -> tuple[FormalTask, ...]:
    raw = schedule.payload.get("tasks")
    if type(raw) is not list:
        raise RuntimeError("formal schedule task list is invalid")
    tasks: list[FormalTask] = []
    for value in raw:
        if type(value) is not dict or set(value) != {
            "task_id",
            "candidate",
            "locus",
        }:
            raise RuntimeError("formal schedule task is invalid")
        tasks.append(
            FormalTask(
                task_id=value["task_id"],
                candidate=CandidateKey.from_value(value["candidate"]),
                locus=value["locus"],
            )
        )
    expected = len(tasks) * len(_SEEDS) * len(_ARMS) * len(_STATES)
    if schedule.payload.get("cell_count") != expected:
        raise RuntimeError("formal schedule cell count is invalid")
    return tuple(tasks)


def _load_pair_if_present(
    root: str | Path, task: FormalTask, seed: int
) -> StableArtifact | None:
    try:
        return load_stable_artifact(
            root,
            artifact_id=f"formal_pair_checkpoint/{task.task_id}/{seed}/current/v1",
            artifact_kind="formal_pair_checkpoint",
            semantic_version="1.0",
        )
    except RuntimeError as error:
        if str(error) in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            return None
        raise


def _validate_pair(
    root: str | Path,
    pair: StableArtifact,
    task: FormalTask,
    seed: int,
    schedule: StableArtifact,
    grant: StableArtifact,
) -> None:
    expected_ids = [
        f"formal_trial/{task.task_id}/{seed}/{arm}/{state}/v1"
        for arm in _ARMS
        for state in _STATES
    ]
    if pair.payload != {
        "task_id": task.task_id,
        "seed": seed,
        "cell_artifact_ids": expected_ids,
        "state": "complete",
    }:
        raise RuntimeError("formal pair checkpoint is invalid")
    if [value.artifact_id for value in pair.inputs] != expected_ids:
        raise RuntimeError("formal pair checkpoint inputs are invalid")
    for arm in _ARMS:
        previous: ArtifactReference | None = None
        for state in _STATES:
            artifact = load_stable_artifact(
                root,
                artifact_id=f"formal_trial/{task.task_id}/{seed}/{arm}/{state}/v1",
                artifact_kind="formal_trial",
                semantic_version="1.0",
            )
            expected_inputs = [schedule.reference, grant.reference]
            if previous is not None:
                expected_inputs.append(previous)
            if artifact.inputs != tuple(expected_inputs):
                raise RuntimeError("formal trial lineage is invalid")
            previous = artifact.reference


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = [
    "FormalRunResult",
    "FormalTask",
    "TrialCapture",
    "run_formal_trials",
    "write_execution_authorization",
    "write_formal_schedule",
]
